# What the two services are deployed from: their packages and a requirements.txt locked by uv.lock, each service with
# only its own dependency group. provisioning/package_source.py assembles it under build/ and hashes it, so a change in
# the code or in the locked dependencies is what triggers a new image and a new runtime version.

data "external" "tool_server_source" {
  program     = ["uv", "run", "--quiet", "python", "-m", "provisioning.package_source"]
  working_dir = local.repository_root

  query = {
    service          = "tools"
    packages         = "backend,shared"
    dependency_group = "tools"
  }
}

data "external" "runtime_source" {
  program     = ["uv", "run", "--quiet", "python", "-m", "provisioning.package_source"]
  working_dir = local.repository_root

  query = {
    service          = "runtime"
    packages         = "agent,shared"
    dependency_group = "agent"
  }
}
