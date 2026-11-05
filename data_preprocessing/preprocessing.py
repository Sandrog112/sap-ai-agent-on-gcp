"""Turn the raw SAP Bikes Sales CSVs into validated, agent-ready tables.

The fixes here come from the findings in data_preprocessing/eda.ipynb. Output is written only after every validation
check passes, so data/processed/ never holds a dataset that failed them.

Usage:
    uv run python data_preprocessing/preprocessing.py
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

RAW_TABLES = (
    "Addresses",
    "BusinessPartners",
    "Employees",
    "ProductCategories",
    "ProductCategoryText",
    "ProductTexts",
    "Products",
    "SalesOrderItems",
    "SalesOrders",
)

# The dataset's "today". The raw data's last activity (2019-07-01) is also a Monday, so the shift preserves weekdays.
AS_OF_DATE = pd.Timestamp("2026-11-02")
# Partners and products are all dated after orders that use them, so they are re-dated to before the first order.
MASTER_DATA_LEAD = pd.Timedelta(days=30)
PLACEHOLDER_DATES = {"29991212", "99991231"}

COUNTRY_FIXES = {"DU": "AE"}
VALID_COUNTRIES = {"AE", "AU", "CA", "DE", "FR", "GB", "IN", "US"}
SALES_ORGS = {"AMER", "APJ", "EMEA"}
STATUS_COLUMNS = ["lifecycle_status", "billing_status", "delivery_status"]
STATUS_CODES = {"C", "I", "X"}
CANCELLED = "X"
COMPLETED = "C"
ENGLISH = "EN"
TAX_RATE = 0.125
# Tighter than the three decimals the amounts use, and absolute: a relative tolerance would hide errors on large orders.
AMOUNT_TOLERANCE = 0.0005

SOURCE_URL = "https://github.com/SAP-samples/datasphere-content"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str
    nullable: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    raw_source: str
    description: str
    key: tuple[str, ...]
    columns: tuple[Column, ...]


def _audit_columns(subject: str) -> tuple[Column, ...]:
    return (
        Column("created_by", "text", f"Employee who created the {subject}."),
        Column("created_at", "date", f"Date the {subject} was created."),
        Column("changed_by", "text", f"Employee who last changed the {subject}."),
        Column("changed_at", "date", f"Date the {subject} was last changed."),
    )


# Single source of truth for the output: drives column order, validation, date shifting and the data dictionary.
SCHEMA = (
    Table(
        "partners",
        "BusinessPartners",
        "Companies that place orders and supply products, with their address.",
        ("partner_id",),
        (
            Column("partner_id", "text", "Partner ID, zero-padded."),
            Column("company_name", "text", "Company name."),
            Column("legal_form", "text", "Legal form, such as Inc. or GmbH."),
            Column("email", "text", "Contact email."),
            Column("phone", "text", "Contact phone number."),
            Column("website", "text", "Company website."),
            Column("street", "text", "Street name."),
            Column("building", "text", "Building number.", nullable=True),
            Column("city", "text", "City."),
            Column("postal_code", "text", "Postal code. Text, because some have leading zeros."),
            Column("country", "text", "ISO 3166 two-letter country code."),
            Column("region", "text", "Sales region: AMER, APJ or EMEA."),
            Column("preferred_currency", "text", "The partner's own currency. Orders are not converted to it."),
            *_audit_columns("partner"),
        ),
    ),
    Table(
        "products",
        "Products",
        "The product catalogue, with English names and category names.",
        ("product_id",),
        (
            Column("product_id", "text", "Product ID, such as RO-1001."),
            Column("name", "text", "Product name."),
            Column("description", "text", "Longer product name. Equals the name where the source had none."),
            Column("category_id", "text", "Two-letter category code."),
            Column("category_name", "text", "Category name, such as Road Bike."),
            Column("supplier_partner_id", "text", "Supplying partner. Blank when the supplier is unknown.", True),
            Column("list_price", "decimal", "Gross price per unit, including 12.5% tax."),
            Column("currency", "text", "Currency of the list price."),
            Column("quantity_unit", "text", "Unit of sale."),
            Column("weight", "decimal", "Weight per unit."),
            Column("weight_unit", "text", "Unit of the weight."),
            *_audit_columns("product"),
        ),
    ),
    Table(
        "employees",
        "Employees",
        "Staff who create and change orders.",
        ("employee_id",),
        (
            Column("employee_id", "text", "Employee ID, zero-padded."),
            Column("first_name", "text", "First name."),
            Column("middle_initial", "text", "Middle initial.", nullable=True),
            Column("last_name", "text", "Last name."),
            Column("email", "text", "Work email."),
            Column("login", "text", "Login name."),
            Column("phone", "text", "Phone number, in its original format."),
            Column("sales_org", "text", "The one sales org the employee works in, from their address region."),
        ),
    ),
    Table(
        "sales_orders",
        "SalesOrders",
        "One row per sales order. Amounts equal the sum of the order's line totals.",
        ("sales_order_id",),
        (
            Column("sales_order_id", "text", "Sales order ID, zero-padded."),
            Column("partner_id", "text", "Partner who placed the order."),
            Column("sales_org", "text", "Sales org: AMER, APJ or EMEA. Always equals the partner's region."),
            Column("fiscal_year_period", "text", "Fiscal period as YYYY0MM, from the creation month."),
            Column("currency", "text", "Currency of the amounts."),
            Column("gross_amount", "decimal", "Order total including tax."),
            Column("net_amount", "decimal", "Order total excluding tax."),
            Column("tax_amount", "decimal", "Tax on the order."),
            Column("lifecycle_status", "text", "Overall status: I, C or X."),
            Column("billing_status", "text", "Billing status: I, C or X."),
            Column("delivery_status", "text", "Delivery status: I, C or X."),
            *_audit_columns("order"),
        ),
    ),
    Table(
        "sales_order_items",
        "SalesOrderItems",
        "One row per order line. A product can repeat on an order, so lines are identified by item number.",
        ("sales_order_id", "item_number"),
        (
            Column("sales_order_id", "text", "Order the line belongs to."),
            Column("item_number", "integer", "Line number on the order: 10, 20, 30 and so on."),
            Column("product_id", "text", "Product on the line."),
            Column("quantity", "integer", "Units ordered."),
            Column("quantity_unit", "text", "Unit of the quantity."),
            Column("currency", "text", "Currency of the amounts."),
            Column("unit_gross_amount", "decimal", "Price per unit including tax."),
            Column("unit_net_amount", "decimal", "Price per unit excluding tax."),
            Column("unit_tax_amount", "decimal", "Tax per unit."),
            Column("line_gross_amount", "decimal", "Unit gross amount × quantity."),
            Column("line_net_amount", "decimal", "Unit net amount × quantity."),
            Column("line_tax_amount", "decimal", "Unit tax amount × quantity."),
            Column("delivery_date", "date", "Delivery date. Blank on cancelled orders.", nullable=True),
        ),
    ),
)

RELATIONSHIPS = (
    ("sales_orders", "partner_id", "partners", "partner_id"),
    ("sales_orders", "created_by", "employees", "employee_id"),
    ("sales_orders", "changed_by", "employees", "employee_id"),
    ("sales_order_items", "sales_order_id", "sales_orders", "sales_order_id"),
    ("sales_order_items", "product_id", "products", "product_id"),
    ("products", "supplier_partner_id", "partners", "partner_id"),
    ("products", "created_by", "employees", "employee_id"),
    ("products", "changed_by", "employees", "employee_id"),
    ("partners", "created_by", "employees", "employee_id"),
    ("partners", "changed_by", "employees", "employee_id"),
)

TYPE_CHECKS = {
    "text": pd.api.types.is_string_dtype,
    "date": pd.api.types.is_datetime64_any_dtype,
    "integer": pd.api.types.is_integer_dtype,
    "decimal": pd.api.types.is_float_dtype,
}


class DataValidationError(ValueError):
    """Raised when the processed tables break a rule, so that nothing gets written."""


# --- Loading ----------------------------------------------------------------------------------------------------------


def _clean_text(series: pd.Series) -> pd.Series:
    """Normalise non-breaking spaces, trim the edges and turn blanks into missing values."""
    cleaned = series.str.replace("\xa0", " ", regex=False).str.strip()
    return cleaned.mask(cleaned.eq(""))


def read_raw_table(path: Path) -> pd.DataFrame:
    """Read one CSV as text, so that zero-padded IDs and postal codes survive."""
    content = path.read_bytes()
    try:
        content.decode("utf-8")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp1252"

    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding=encoding).apply(_clean_text)

    unnamed = frame.columns.str.startswith("Unnamed:")
    if frame.loc[:, unnamed].notna().any(axis=None):
        raise DataValidationError(f"{path.name}: a column with no header contains data")
    return frame.loc[:, ~unnamed]


def load_raw(raw_dir: Path) -> dict[str, pd.DataFrame]:
    unexpected = {path.stem for path in raw_dir.glob("*.csv")} - set(RAW_TABLES)
    if unexpected:
        logger.warning("Ignoring unexpected raw files: %s", sorted(unexpected))
    return {name: read_raw_table(raw_dir / f"{name}.csv") for name in RAW_TABLES}


def _parse_dates(series: pd.Series) -> pd.Series:
    """Parse YYYYMMDD text, treating far-future placeholders as missing. Any other bad value raises an error."""
    return pd.to_datetime(series.mask(series.isin(PLACEHOLDER_DATES)), format="%Y%m%d")


# --- Building the tables ----------------------------------------------------------------------------------------------


def build_partners(raw: dict[str, pd.DataFrame], master_date: pd.Timestamp) -> pd.DataFrame:
    partners = (
        raw["BusinessPartners"]
        .merge(raw["Addresses"], on="ADDRESSID", how="left", validate="one_to_one")
        .rename(
            columns={
                "PARTNERID": "partner_id",
                "COMPANYNAME": "company_name",
                "LEGALFORM": "legal_form",
                "EMAILADDRESS": "email",
                "PHONENUMBER": "phone",
                "WEBADDRESS": "website",
                "STREET": "street",
                "BUILDING": "building",
                "CITY": "city",
                "POSTALCODE": "postal_code",
                "COUNTRY": "country",
                "REGION": "region",
                "CURRENCY": "preferred_currency",
                "CREATEDBY": "created_by",
                "CHANGEDBY": "changed_by",
            }
        )
    )
    corrected = partners["country"].isin(COUNTRY_FIXES)
    partners["country"] = partners["country"].replace(COUNTRY_FIXES)
    logger.info("partners: corrected the country code on %d rows", corrected.sum())
    return partners.assign(created_at=master_date, changed_at=master_date)


def build_products(raw: dict[str, pd.DataFrame], partners: pd.DataFrame, master_date: pd.Timestamp) -> pd.DataFrame:
    product_texts = raw["ProductTexts"].loc[raw["ProductTexts"]["LANGUAGE"].eq(ENGLISH)]
    category_texts = raw["ProductCategoryText"].loc[raw["ProductCategoryText"]["LANGUAGE"].eq(ENGLISH)]

    products = (
        raw["Products"]
        .merge(
            product_texts[["PRODUCTID", "SHORT_DESCR", "MEDIUM_DESCR"]],
            on="PRODUCTID",
            how="left",
            validate="one_to_one",
        )
        .merge(
            category_texts[["PRODCATEGORYID", "SHORT_DESCR"]].rename(columns={"SHORT_DESCR": "category_name"}),
            on="PRODCATEGORYID",
            how="left",
            validate="many_to_one",
        )
        .rename(
            columns={
                "PRODUCTID": "product_id",
                "SHORT_DESCR": "name",
                "PRODCATEGORYID": "category_id",
                "SUPPLIER_PARTNERID": "supplier_partner_id",
                "CURRENCY": "currency",
                "QUANTITYUNIT": "quantity_unit",
                "WEIGHTUNIT": "weight_unit",
                "CREATEDBY": "created_by",
                "CHANGEDBY": "changed_by",
            }
        )
    )
    products["description"] = products["MEDIUM_DESCR"].fillna(products["name"])
    products["list_price"] = products["PRICE"].astype("float64")
    products["weight"] = products["WEIGHTMEASURE"].astype("float64")

    known_supplier = products["supplier_partner_id"].isin(partners["partner_id"])
    products["supplier_partner_id"] = products["supplier_partner_id"].where(known_supplier)
    logger.info("products: cleared the supplier on %d rows with an unknown partner", (~known_supplier).sum())
    return products.assign(created_at=master_date, changed_at=master_date)


def build_employees(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    employees = (
        raw["Employees"]
        .merge(raw["Addresses"][["ADDRESSID", "REGION"]], on="ADDRESSID", how="left", validate="one_to_one")
        .rename(
            columns={
                "EMPLOYEEID": "employee_id",
                "NAME_FIRST": "first_name",
                "NAME_MIDDLE": "middle_initial",
                "NAME_LAST": "last_name",
                "EMAILADDRESS": "email",
                "LOGINNAME": "login",
                "PHONENUMBER": "phone",
                "REGION": "sales_org",
            }
        )
    )
    employees["login"] = employees["login"].str.replace(r"\s+", "", regex=True)
    return employees


def build_sales_orders(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    orders = raw["SalesOrders"].rename(
        columns={
            "SALESORDERID": "sales_order_id",
            "PARTNERID": "partner_id",
            "SALESORG": "sales_org",
            "CURRENCY": "currency",
            "LIFECYCLESTATUS": "lifecycle_status",
            "BILLINGSTATUS": "billing_status",
            "DELIVERYSTATUS": "delivery_status",
            "CREATEDBY": "created_by",
            "CHANGEDBY": "changed_by",
        }
    )
    # Items look up their order's dates by ID below, so duplicates must be caught before validation runs.
    if orders["sales_order_id"].duplicated().any():
        raise DataValidationError("SalesOrders.csv: duplicate order IDs")

    for source, target in {"GROSSAMOUNT": "gross", "NETAMOUNT": "net", "TAXAMOUNT": "tax"}.items():
        orders[f"{target}_amount"] = orders[source].astype("float64")
    orders["created_at"] = _parse_dates(orders["CREATEDAT"])
    orders["changed_at"] = _parse_dates(orders["CHANGEDAT"])
    return orders


def build_sales_order_items(raw: dict[str, pd.DataFrame], orders: pd.DataFrame) -> pd.DataFrame:
    items = raw["SalesOrderItems"].rename(
        columns={
            "SALESORDERID": "sales_order_id",
            "PRODUCTID": "product_id",
            "QUANTITYUNIT": "quantity_unit",
            "CURRENCY": "currency",
        }
    )
    items["item_number"] = items["SALESORDERITEM"].astype("int64")
    items["quantity"] = items["QUANTITY"].astype("int64")

    # The source stores these per unit, despite their names. Order totals only reconcile once multiplied by quantity.
    for source, target in {"GROSSAMOUNT": "gross", "NETAMOUNT": "net", "TAXAMOUNT": "tax"}.items():
        items[f"unit_{target}_amount"] = items[source].astype("float64")
        items[f"line_{target}_amount"] = items[f"unit_{target}_amount"] * items["quantity"]

    delivery = _parse_dates(items["DELIVERYDATE"])
    order_dates = orders.set_index("sales_order_id")[["created_at", "changed_at"]]
    created_at = items["sales_order_id"].map(order_dates["created_at"])
    changed_at = items["sales_order_id"].map(order_dates["changed_at"])
    before_creation = delivery.lt(created_at)
    items["delivery_date"] = delivery.mask(before_creation, changed_at)
    logger.info(
        "sales_order_items: moved %d delivery dates to after creation, on %d orders",
        before_creation.sum(),
        items.loc[before_creation, "sales_order_id"].nunique(),
    )
    logger.info("sales_order_items: blanked %d placeholder delivery dates", delivery.isna().sum())
    return items


def build_tables(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    orders = build_sales_orders(raw)
    master_date = orders["created_at"].min() - MASTER_DATA_LEAD
    partners = build_partners(raw, master_date)

    tables = {
        "partners": partners,
        "products": build_products(raw, partners, master_date),
        "employees": build_employees(raw),
        "sales_orders": orders,
        "sales_order_items": build_sales_order_items(raw, orders),
    }

    shift = AS_OF_DATE - orders["changed_at"].max()
    for spec in SCHEMA:
        for column in spec.columns:
            if column.type == "date":
                tables[spec.name][column.name] = tables[spec.name][column.name] + shift
    logger.info("Shifted all dates forward by %d days, so the latest activity is %s", shift.days, AS_OF_DATE.date())

    orders["fiscal_year_period"] = orders["created_at"].dt.strftime("%Y0%m")

    return {
        spec.name: tables[spec.name][[column.name for column in spec.columns]]
        .sort_values(list(spec.key))
        .reset_index(drop=True)
        for spec in SCHEMA
    }


# --- Validation -------------------------------------------------------------------------------------------------------
# Each check returns {rule: number of violations}, so that one run reports every problem at once.


def _matches(left, right) -> np.ndarray:
    return np.isclose(left, right, rtol=0, atol=AMOUNT_TOLERANCE)


def check_schema(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    problems = {}
    for spec in SCHEMA:
        frame = tables[spec.name]
        problems[f"{spec.name}: duplicate keys"] = int(frame.duplicated(list(spec.key)).sum())
        for column in spec.columns:
            values = frame[column.name]
            problems[f"{spec.name}.{column.name}: wrong type"] = int(not TYPE_CHECKS[column.type](values))
            if not column.nullable:
                problems[f"{spec.name}.{column.name}: missing values"] = int(values.isna().sum())
            if column.type == "text":
                dirty = values.dropna().ne(values.dropna().str.strip()) | values.dropna().str.contains("\xa0")
                problems[f"{spec.name}.{column.name}: untrimmed text"] = int(dirty.sum())
    return problems


def check_against_raw(tables: dict[str, pd.DataFrame], raw: dict[str, pd.DataFrame]) -> dict[str, int]:
    problems = {
        f"{spec.name}: row count differs from {spec.raw_source}": int(
            len(tables[spec.name]) != len(raw[spec.raw_source])
        )
        for spec in SCHEMA
    }
    known_categories = raw["ProductCategories"]["PRODCATEGORYID"]
    problems["products: category not in ProductCategories"] = int(
        (~tables["products"]["category_id"].isin(known_categories)).sum()
    )
    return problems


def check_relationships(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    problems = {}
    for child, child_column, parent, parent_column in RELATIONSHIPS:
        values = tables[child][child_column].dropna()
        orphans = ~values.isin(tables[parent][parent_column])
        problems[f"{child}.{child_column}: not found in {parent}.{parent_column}"] = int(orphans.sum())
    return problems


def check_amounts(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    orders, items = tables["sales_orders"], tables["sales_order_items"]
    list_price = items["product_id"].map(tables["products"].set_index("product_id")["list_price"])
    line_columns = ["line_gross_amount", "line_net_amount", "line_tax_amount"]
    totals = items.groupby("sales_order_id")[line_columns].sum().reindex(orders["sales_order_id"])

    problems = {
        "items: quantity is not positive": int(items["quantity"].le(0).sum()),
        "items: unit amount is not positive": int(items["unit_net_amount"].le(0).sum()),
        "items: net + tax differs from gross": int(
            (~_matches(items["unit_net_amount"] + items["unit_tax_amount"], items["unit_gross_amount"])).sum()
        ),
        "items: tax is not 12.5% of gross": int(
            (~_matches(items["unit_tax_amount"], items["unit_gross_amount"] * TAX_RATE)).sum()
        ),
        "items: unit gross differs from list price": int((~_matches(items["unit_gross_amount"], list_price)).sum()),
    }
    for kind in ("gross", "net", "tax"):
        problems[f"orders: {kind} differs from the sum of its lines"] = int(
            (~_matches(orders[f"{kind}_amount"].to_numpy(), totals[f"line_{kind}_amount"].to_numpy())).sum()
        )
    return problems


def check_statuses(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    statuses = tables["sales_orders"][STATUS_COLUMNS]
    cancelled = statuses.eq(CANCELLED)
    return {
        "orders: unknown status code": int((~statuses.isin(STATUS_CODES)).any(axis=1).sum()),
        "orders: cancelled in some status columns only": int((cancelled.any(axis=1) & ~cancelled.all(axis=1)).sum()),
    }


def check_dates(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    orders, items = tables["sales_orders"], tables["sales_order_items"]
    by_order = orders.set_index("sales_order_id")
    created_at = items["sales_order_id"].map(by_order["created_at"])
    lifecycle = items["sales_order_id"].map(by_order["lifecycle_status"])
    delivery_status = items["sales_order_id"].map(by_order["delivery_status"])
    delivery = items["delivery_date"]
    first_order = orders["created_at"].min()

    problems = {
        "orders: latest activity is not the as-of date": int(orders["changed_at"].max() != AS_OF_DATE),
        "orders: changed before created": int(orders["changed_at"].lt(orders["created_at"]).sum()),
        "orders: fiscal period differs from creation month": int(
            orders["fiscal_year_period"].ne(orders["created_at"].dt.strftime("%Y0%m")).sum()
        ),
        "items: delivered before the order was created": int(delivery.lt(created_at).sum()),
        "items: marked delivered but dated after the as-of date": int(
            (delivery_status.eq(COMPLETED) & delivery.gt(AS_OF_DATE)).sum()
        ),
        "items: delivery date on a cancelled order": int((lifecycle.eq(CANCELLED) & delivery.notna()).sum()),
        "items: no delivery date on a live order": int((lifecycle.ne(CANCELLED) & delivery.isna()).sum()),
    }
    for table in ("partners", "products"):
        for column in ("created_at", "changed_at"):
            problems[f"{table}.{column}: not before the first order"] = int(tables[table][column].ge(first_order).sum())
    return problems


def check_reference_values(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    partners, employees = tables["partners"], tables["employees"]
    orders, items = tables["sales_orders"], tables["sales_order_items"]
    partner_region = orders["partner_id"].map(partners.set_index("partner_id")["region"])
    creator_org = orders["created_by"].map(employees.set_index("employee_id")["sales_org"])
    order_currency = items["sales_order_id"].map(orders.set_index("sales_order_id")["currency"])
    return {
        "partners: invalid country code": int((~partners["country"].isin(VALID_COUNTRIES)).sum()),
        "partners: unknown region": int((~partners["region"].isin(SALES_ORGS)).sum()),
        "employees: login contains whitespace": int(employees["login"].str.contains(r"\s").sum()),
        "orders: unknown sales org": int((~orders["sales_org"].isin(SALES_ORGS)).sum()),
        "orders: sales org differs from the partner's region": int(orders["sales_org"].ne(partner_region).sum()),
        "orders: sales org differs from the creator's": int(orders["sales_org"].ne(creator_org).sum()),
        "items: currency differs from the order's": int(items["currency"].ne(order_currency).sum()),
    }


def validate(tables: dict[str, pd.DataFrame], raw: dict[str, pd.DataFrame]) -> int:
    """Raise DataValidationError listing every broken rule. Returns the number of rules checked."""
    results = {
        **check_schema(tables),
        **check_against_raw(tables, raw),
        **check_relationships(tables),
        **check_amounts(tables),
        **check_statuses(tables),
        **check_dates(tables),
        **check_reference_values(tables),
    }
    failures = [f"{rule} ({count})" for rule, count in results.items() if count]
    if failures:
        raise DataValidationError("Processed data failed validation:\n- " + "\n- ".join(failures))
    return len(results)


# --- Output -----------------------------------------------------------------------------------------------------------


def _data_dictionary(tables: dict[str, pd.DataFrame]) -> str:
    lines = [
        "# Data dictionary",
        "",
        "*Generated by `data_preprocessing/preprocessing.py`. Edit the schema there, not this file.*",
        "",
        f'**As-of date:** {AS_OF_DATE.date()}. Treat this as "today" when working out what is recent or overdue.',
        "",
        "## Status codes",
        "",
        "| Code | Meaning |",
        "|---|---|",
        "| `I` | In progress |",
        "| `C` | Completed |",
        "| `X` | Cancelled |",
        "",
        "## Formats",
        "",
        "- Files are UTF-8 CSV. A blank cell means no value.",
        "- Dates are `YYYY-MM-DD`. Amounts have three decimals, because rounding to two breaks the order totals.",
        "- IDs and postal codes are text. Load them as text, or their leading zeros will be lost.",
        "",
        "## Source and changes",
        "",
        f"Derived from SAP's Bikes Sales sample in [datasphere-content]({SOURCE_URL}), © SAP SE or an SAP affiliate "
        "company, licensed under Apache-2.0 (see `data/raw/LICENSE`). The changes from the original are:",
        "",
        "- Merged addresses into partners, and product texts and categories into products, in English.",
        "- Dropped empty and uninformative columns, including `PARTNERROLE`, which did not separate partners.",
        "- Renamed item amounts as unit amounts, because they are per unit, and added line totals.",
        "- Moved all dates forward by a fixed number of days and recalculated fiscal periods.",
        "- Re-dated partners and products to before the first order.",
        "- Moved delivery dates that came before the order's creation to the order's last-change date.",
        "- Blanked placeholder delivery dates on cancelled orders, and suppliers that don't exist.",
        "- Corrected the country code `DU` to `AE`, and cleaned stray spaces from text.",
        "- Derived `sales_org` for employees from their address region.",
        "",
        "No partners, products, employees, orders or order lines were added or removed.",
    ]
    for spec in SCHEMA:
        lines += [
            "",
            f"## {spec.name}",
            "",
            f"{spec.description} {len(tables[spec.name]):,} rows. Key: `{' + '.join(spec.key)}`.",
            "",
            "| Column | Type | Can be blank | Description |",
            "|---|---|---|---|",
        ]
        for column in spec.columns:
            blank = "Yes" if column.nullable else "No"
            lines.append(f"| `{column.name}` | {column.type} | {blank} | {column.description} |")
    return "\n".join(lines) + "\n"


def save(tables: dict[str, pd.DataFrame], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_csv(
            output_dir / f"{name}.csv",
            index=False,
            encoding="utf-8",
            lineterminator="\n",
            date_format="%Y-%m-%d",
            float_format="%.3f",
        )
    (output_dir / "DATA_DICTIONARY.md").write_text(_data_dictionary(tables), encoding="utf-8")


def run(raw_dir: Path = RAW_DIR, output_dir: Path = PROCESSED_DIR) -> dict[str, pd.DataFrame]:
    raw = load_raw(raw_dir)
    logger.info("Loaded %d raw tables, %s rows", len(raw), f"{sum(map(len, raw.values())):,}")

    tables = build_tables(raw)
    rules = validate(tables, raw)
    logger.info("Validation passed: %d rules checked", rules)

    save(tables, output_dir)
    for name, frame in tables.items():
        logger.info("Saved %-18s %5d rows, %2d columns", name, *frame.shape)
    return tables


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run()
