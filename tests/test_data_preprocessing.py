"""The data pipeline (data_preprocessing/): reading the raw CSVs, the fixes that turn them into the processed tables,
the validation that guards the output, and the Cloud SQL loader. Runs on the real files in data/raw; the loader tests
need a PostgreSQL server, see tests/conftest.py."""

import csv
import filecmp
import shutil
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pg8000.dbapi
import pytest
import upload_to_gcp
from preprocessing import (
    AS_OF_DATE,
    PLACEHOLDER_DATES,
    PROCESSED_DIR,
    RAW_DIR,
    RELATIONSHIPS,
    SCHEMA,
    VALID_COUNTRIES,
    DataValidationError,
    _parse_dates,
    build_tables,
    load_raw,
    read_raw_table,
    run,
    validate,
)

Tables = dict[str, pd.DataFrame]
SPECS = {spec.name: spec for spec in SCHEMA}


@pytest.fixture(scope="module")
def raw() -> Tables:
    return load_raw(RAW_DIR)


@pytest.fixture(scope="module")
def tables(raw: Tables) -> Tables:
    return build_tables(raw)


# --- Reading the raw files --------------------------------------------------------------------------------------------


def write(path: Path, content: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(content.encode(encoding))
    return path


def test_ids_and_postal_codes_keep_their_leading_zeros(tmp_path):
    frame = read_raw_table(write(tmp_path / "t.csv", "PARTNERID,POSTALCODE\n0100000000,02139\n"))

    assert frame.iloc[0].tolist() == ["0100000000", "02139"]


def test_text_is_trimmed_and_blank_cells_become_missing(tmp_path):
    frame = read_raw_table(write(tmp_path / "t.csv", "NAME,BUILDING\n\xa0Bike\xa0World \xa0,  \n"))

    assert frame.loc[0, "NAME"] == "Bike World"
    assert pd.isna(frame.loc[0, "BUILDING"])


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_files_are_read_in_the_encodings_the_source_uses(tmp_path, encoding):
    frame = read_raw_table(write(tmp_path / "t.csv", "NAME\nMüller\n", encoding))

    assert frame.columns.tolist() == ["NAME"]  # a byte order mark is not part of the header
    assert frame.loc[0, "NAME"] == "Müller"


def test_empty_headerless_columns_are_dropped_but_data_in_one_is_an_error(tmp_path):
    assert read_raw_table(write(tmp_path / "ok.csv", "ID,\n1,\n")).columns.tolist() == ["ID"]
    with pytest.raises(DataValidationError, match="no header"):
        read_raw_table(write(tmp_path / "bad.csv", "ID,\n1,stray\n"))


def test_placeholder_dates_are_missing_and_malformed_dates_are_errors():
    parsed = _parse_dates(pd.Series(["20180111", *sorted(PLACEHOLDER_DATES)]))

    assert parsed.tolist()[0] == pd.Timestamp("2018-01-11")
    assert parsed.isna().tolist() == [False, True, True]
    with pytest.raises(ValueError):
        _parse_dates(pd.Series(["2018-01-11"]))


# --- The fixes --------------------------------------------------------------------------------------------------------


def test_pipeline_reproduces_the_committed_dataset(tmp_path):
    """data/processed is exactly what the pipeline writes from data/raw, so neither can drift from the other."""
    run(output_dir=tmp_path)

    committed = sorted(path.name for path in PROCESSED_DIR.iterdir())
    assert committed == ["DATA_DICTIONARY.md", *sorted(f"{name}.csv" for name in SPECS)]
    assert filecmp.cmpfiles(PROCESSED_DIR, tmp_path, committed, shallow=False) == (committed, [], [])


def test_no_rows_are_added_or_removed_and_columns_follow_the_schema(raw, tables):
    for spec in SCHEMA:
        assert len(tables[spec.name]) == len(raw[spec.raw_source])
        assert tables[spec.name].columns.tolist() == [column.name for column in spec.columns]


def test_dates_move_forward_by_whole_weeks_to_the_as_of_date(raw, tables):
    orders = tables["sales_orders"]
    shift = AS_OF_DATE - pd.Timestamp(raw["SalesOrders"]["CHANGEDAT"].max())

    assert orders["changed_at"].max() == AS_OF_DATE
    assert shift.days % 7 == 0  # so every date keeps its weekday
    assert orders["created_at"].min() == pd.Timestamp(raw["SalesOrders"]["CREATEDAT"].min()) + shift
    first = orders.iloc[0]
    assert first["fiscal_year_period"] == f"{first['created_at'].year}0{first['created_at'].month:02d}"


def test_partners_and_products_are_dated_before_the_first_order(tables):
    first_order = tables["sales_orders"]["created_at"].min()

    for name in ("partners", "products"):
        assert tables[name][["created_at", "changed_at"]].lt(first_order).all(axis=None)


def test_country_code_du_is_corrected(raw, tables):
    partner_addresses = raw["Addresses"]["ADDRESSID"].isin(raw["BusinessPartners"]["ADDRESSID"])

    assert raw["Addresses"].loc[partner_addresses, "COUNTRY"].eq("DU").sum() == 5
    assert tables["partners"]["country"].eq("AE").sum() == 5
    assert set(tables["partners"]["country"]) <= VALID_COUNTRIES


def test_suppliers_that_are_not_partners_are_cleared(raw, tables):
    suppliers = tables["products"]["supplier_partner_id"]

    assert raw["Products"]["SUPPLIER_PARTNERID"].notna().all()
    assert suppliers.isna().sum() == 2
    assert suppliers.dropna().isin(tables["partners"]["partner_id"]).all()


def test_item_amounts_are_per_unit_and_line_totals_are_added(raw, tables):
    source = raw["SalesOrderItems"].iloc[0]
    item = tables["sales_order_items"].iloc[0]

    assert (source["SALESORDERID"], source["SALESORDERITEM"]) == ("0500000000", "0000000010")
    assert (item["sales_order_id"], item["item_number"]) == ("0500000000", 10)
    assert item["unit_gross_amount"] == float(source["GROSSAMOUNT"]) == 2499.0
    assert item["line_gross_amount"] == 2499.0 * int(source["QUANTITY"]) == 9996.0


def test_delivery_dates_before_the_order_move_to_its_last_change(raw, tables):
    source = raw["SalesOrderItems"]
    created = source["SALESORDERID"].map(raw["SalesOrders"].set_index("SALESORDERID")["CREATEDAT"])
    too_early = source.loc[source["DELIVERYDATE"].lt(created) & ~source["DELIVERYDATE"].isin(PLACEHOLDER_DATES)]
    moved_keys = pd.MultiIndex.from_arrays([too_early["SALESORDERID"], too_early["SALESORDERITEM"].astype(int)])

    items = tables["sales_order_items"].set_index(["sales_order_id", "item_number"])
    changed_at = tables["sales_orders"].set_index("sales_order_id")["changed_at"]
    moved = items.loc[moved_keys, "delivery_date"]

    assert len(moved) == 52
    assert moved.eq(moved.index.get_level_values(0).map(changed_at).to_numpy()).all()


def test_only_cancelled_orders_have_no_delivery_date(tables):
    items = tables["sales_order_items"]
    cancelled = tables["sales_orders"].loc[tables["sales_orders"]["lifecycle_status"].eq("X"), "sales_order_id"]

    assert items["delivery_date"].isna().sum() == 22
    assert items["delivery_date"].isna().eq(items["sales_order_id"].isin(cancelled)).all()


def test_logins_lose_their_stray_whitespace(raw, tables):
    assert raw["Employees"]["LOGINNAME"].str.contains(r"\s").sum() == 1
    assert not tables["employees"]["login"].str.contains(r"\s").any()


def test_duplicate_order_ids_are_rejected(raw):
    duplicated = {**raw, "SalesOrders": pd.concat([raw["SalesOrders"], raw["SalesOrders"].head(1)])}

    with pytest.raises(DataValidationError, match="duplicate order IDs"):
        build_tables(duplicated)


# --- Validation -------------------------------------------------------------------------------------------------------


def test_processed_data_passes_every_rule(tables, raw):
    assert validate(tables, raw) > 200


def break_order_total(tables: Tables) -> None:
    tables["sales_orders"].loc[0, "gross_amount"] += 1


def break_partner_reference(tables: Tables) -> None:
    tables["sales_orders"].loc[0, "partner_id"] = "0199999999"


def break_item_key(tables: Tables) -> None:
    items = tables["sales_order_items"]
    tables["sales_order_items"] = pd.concat([items, items.head(1)], ignore_index=True)


def break_required_value(tables: Tables) -> None:
    tables["partners"].loc[0, "company_name"] = None


def break_text(tables: Tables) -> None:
    tables["partners"].loc[0, "city"] = " Berlin"


def break_type(tables: Tables) -> None:
    tables["sales_order_items"]["quantity"] = tables["sales_order_items"]["quantity"].astype("float64")


def break_status_code(tables: Tables) -> None:
    tables["sales_orders"].loc[0, "billing_status"] = "Z"


def break_cancellation(tables: Tables) -> None:
    tables["sales_orders"].loc[0, "billing_status"] = "X"  # of an order that is completed otherwise


def break_tax(tables: Tables) -> None:
    tables["sales_order_items"].loc[0, "unit_tax_amount"] += 1


def break_list_price(tables: Tables) -> None:
    tables["products"].loc[0, "list_price"] += 1


def break_delivery_date(tables: Tables) -> None:
    tables["sales_order_items"].loc[0, "delivery_date"] = pd.Timestamp("2000-01-01")


def break_change_date(tables: Tables) -> None:
    orders = tables["sales_orders"]
    orders.loc[0, "changed_at"] = orders.loc[0, "created_at"] - pd.Timedelta(days=1)


def break_country(tables: Tables) -> None:
    tables["partners"].loc[0, "country"] = "DU"


@pytest.mark.parametrize(
    ("damage", "rule"),
    [
        (break_order_total, "orders: gross differs from the sum of its lines (1)"),
        (break_partner_reference, "sales_orders.partner_id: not found in partners.partner_id (1)"),
        (break_item_key, "sales_order_items: duplicate keys (1)"),
        (break_required_value, "partners.company_name: missing values (1)"),
        (break_text, "partners.city: untrimmed text (1)"),
        (break_type, "sales_order_items.quantity: wrong type (1)"),
        (break_status_code, "orders: unknown status code (1)"),
        (break_cancellation, "orders: cancelled in some status columns only (1)"),
        (break_tax, "items: tax is not 12.5% of gross (1)"),
        (break_list_price, "items: unit gross differs from list price"),
        (break_delivery_date, "items: delivered before the order was created (1)"),
        (break_change_date, "orders: changed before created (1)"),
        (break_country, "partners: invalid country code (1)"),
    ],
)
def test_validation_names_the_broken_rule(tables, raw, damage: Callable[[Tables], None], rule: str):
    damaged = {name: frame.copy() for name, frame in tables.items()}
    damage(damaged)

    with pytest.raises(DataValidationError) as error:
        validate(damaged, raw)
    assert rule in str(error.value)


def test_validation_reports_every_problem_at_once(tables, raw):
    damaged = {name: frame.copy() for name, frame in tables.items()}
    break_order_total(damaged)
    break_text(damaged)

    with pytest.raises(DataValidationError) as error:
        validate(damaged, raw)
    assert "orders: gross differs" in str(error.value) and "partners.city: untrimmed text" in str(error.value)


def rewrite_csv(path: Path, change: Callable[[list[dict[str, str]]], None]) -> None:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_nothing_is_written_when_validation_fails(tmp_path):
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "processed"
    shutil.copytree(RAW_DIR, raw_dir)
    rewrite_csv(raw_dir / "SalesOrders.csv", lambda rows: rows[0].update(GROSSAMOUNT="1"))

    with pytest.raises(DataValidationError, match="gross differs from the sum of its lines"):
        run(raw_dir, output_dir)
    assert not output_dir.exists()


# --- The Cloud SQL loader (upload_to_gcp.py) --------------------------------------------------------------------------


def test_tables_are_created_with_types_keys_and_rules():
    orders = upload_to_gcp.create_table_statements(SPECS["sales_orders"])[0]
    items = upload_to_gcp.create_table_statements(SPECS["sales_order_items"])[0]

    assert "sales_order_id TEXT NOT NULL" in orders
    assert "gross_amount NUMERIC(14, 3) NOT NULL" in orders  # exact, unlike a float
    assert "PRIMARY KEY (sales_order_id)" in orders
    assert "CHECK (lifecycle_status IN ('C', 'I', 'X'))" in orders
    assert "CHECK (changed_at >= created_at)" in orders
    assert "delivery_date DATE,\n" in items  # blank on cancelled orders, so not required
    assert "PRIMARY KEY (sales_order_id, item_number)" in items
    assert "CHECK (quantity > 0)" in items


def test_data_dictionary_is_stored_as_comments():
    spec = SPECS["partners"]

    comments = upload_to_gcp.create_table_statements(spec)[1:]

    assert len(comments) == 1 + len(spec.columns)
    assert comments[0].startswith("COMMENT ON TABLE partners IS 'Companies that place orders")
    assert "COMMENT ON COLUMN partners.preferred_currency IS 'The partner''s own currency." in "\n".join(comments)


def test_every_relationship_becomes_a_foreign_key():
    statements = upload_to_gcp.foreign_key_statements()

    assert len(statements) == len(RELATIONSHIPS)
    assert "ALTER TABLE sales_order_items ADD FOREIGN KEY (product_id) REFERENCES products (product_id)" in statements


def fetch(connection, sql: str) -> list:
    cursor = connection.cursor()
    cursor.execute(sql)
    return list(cursor.fetchall())


def test_load_fills_every_table_and_commits(empty_database, tables):
    rows = upload_to_gcp.load(empty_database)
    empty_database.rollback()  # whatever was not committed is gone

    assert rows == {name: len(frame) for name, frame in tables.items()}
    assert fetch(empty_database, "SELECT gross_amount::text, created_at::text FROM sales_orders LIMIT 1") == [
        ["13587.000", "2025-05-15"]
    ]
    assert fetch(empty_database, "SELECT col_description('sales_orders'::regclass, 1)") == [
        ["Sales order ID, zero-padded."]
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE sales_order_items SET product_id = 'XX-0000' WHERE sales_order_id = '0500000000'",  # no such product
        "UPDATE sales_order_items SET quantity = 0 WHERE sales_order_id = '0500000000'",
        "UPDATE sales_orders SET lifecycle_status = 'Z' WHERE sales_order_id = '0500000000'",
        "UPDATE sales_orders SET changed_at = created_at - 1 WHERE sales_order_id = '0500000000'",
        "UPDATE sales_orders SET partner_id = NULL WHERE sales_order_id = '0500000000'",
        "DELETE FROM partners",  # orders refer to them
    ],
)
def test_database_keeps_enforcing_the_rules_after_the_load(empty_database, statement):
    upload_to_gcp.load(empty_database)

    with pytest.raises(pg8000.dbapi.DatabaseError):
        empty_database.cursor().execute(statement)


def test_existing_tables_are_only_replaced_on_request(empty_database):
    upload_to_gcp.load(empty_database)
    cursor = empty_database.cursor()
    cursor.execute("UPDATE partners SET phone = 'changed by the agent'")
    empty_database.commit()

    with pytest.raises(upload_to_gcp.LoadError, match="--replace"):
        upload_to_gcp.load(empty_database)
    assert fetch(empty_database, "SELECT count(DISTINCT phone) FROM partners") == [[1]]  # still the agent's change

    upload_to_gcp.load(empty_database, replace=True)
    assert fetch(empty_database, "SELECT count(DISTINCT phone) FROM partners") == [[40]]


def test_failed_load_leaves_the_database_as_it_was(empty_database, tmp_path):
    data_dir = tmp_path / "processed"
    shutil.copytree(PROCESSED_DIR, data_dir)
    rewrite_csv(data_dir / "sales_orders.csv", lambda rows: rows[0].update(gross_amount="1.000"))

    with pytest.raises(upload_to_gcp.LoadError, match="1 orders don't equal the sum of their lines"):
        upload_to_gcp.load(empty_database, data_dir)
    assert fetch(empty_database, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [[0]]
