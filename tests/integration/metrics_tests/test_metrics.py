#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import time
from pathlib import Path

import pytest
import urllib3
import yaml
from pytest_operator.plugin import OpsTest

from ..ha_tests import helpers as ha_helpers

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
DATABASE_APP_NAME = "mongodb-k8s"
MONGODB_EXPORTER_PORT = 9216
MEDIAN_REELECTION_TIME = 12


@pytest.fixture(scope="module")
def chaos_mesh(ops_test: OpsTest) -> None:
    ha_helpers.deploy_chaos_mesh(ops_test.model.info.name)

    yield

    ha_helpers.destroy_chaos_mesh(ops_test.model.info.name)


async def get_address(ops_test: OpsTest, app_name=DATABASE_APP_NAME, unit_num=0) -> str:
    """Get the address for a unit."""
    status = await ops_test.model.get_status()  # noqa: F821
    address = status["applications"][app_name]["units"][f"{app_name}/{unit_num}"]["address"]
    return address


async def verify_endpoints(ops_test: OpsTest, app_name=DATABASE_APP_NAME):
    """Verifies mongodb endpoint is functional on a given unit."""
    http = urllib3.PoolManager()

    for unit_id in range(len(ops_test.model.applications[app_name].units)):
        app_address = await get_address(ops_test=ops_test, app_name=app_name, unit_num=unit_id)
        mongo_resp = http.request("GET", f"http://{app_address}:{MONGODB_EXPORTER_PORT}/metrics")

    assert mongo_resp.status == 200

    # if configured correctly there should be more than one mongodb metric present
    mongodb_metrics = mongo_resp._body.decode("utf8")
    assert mongodb_metrics.count("mongo") > 10


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest) -> None:
    """Build and deploy three units of MongoDB and one unit of TLS."""
    # no need to build and deploy charm if provided
    mongodb_application_name = await ha_helpers.get_application_name(ops_test, DATABASE_APP_NAME)
    if mongodb_application_name:
        return

    async with ops_test.fast_forward():
        my_charm = await ops_test.build_charm(".")
        resources = {"mongodb-image": METADATA["resources"]["mongodb-image"]["upstream-source"]}
        await ops_test.model.deploy(my_charm, num_units=3, resources=resources, series="jammy")
        await ops_test.model.wait_for_idle(apps=[DATABASE_APP_NAME], status="active", timeout=2000)


async def test_endpoints(ops_test: OpsTest):
    """Sanity check that endpoints are running."""
    mongodb_application_name = await ha_helpers.get_application_name(ops_test, DATABASE_APP_NAME)
    await verify_endpoints(ops_test, mongodb_application_name)


async def test_endpoints_network_cut(ops_test: OpsTest, chaos_mesh):
    """Verify that endpoint still function correctly after a network cut."""
    # retrieve a primary unit and a non-primary unit (active-unit). The primary unit will have its
    # network disrupted, while the active unit allows us to communicate to `mongod`
    mongodb_application_name = await ha_helpers.get_application_name(ops_test, DATABASE_APP_NAME)
    primary = await ha_helpers.get_replica_set_primary(ops_test)
    active_unit = [
        unit
        for unit in ops_test.model.applications[mongodb_application_name].units
        if unit.name != primary.name
    ][0]

    # Create networkchaos policy to isolate instance from cluster - ie cut network
    ha_helpers.isolate_instance_from_cluster(ops_test, primary.name)

    # sleep for twice the median election time
    time.sleep(MEDIAN_REELECTION_TIME * 2)

    # Remove networkchaos policy isolating instance from cluster - ie resolve network
    ha_helpers.remove_instance_isolation(ops_test)

    # Wait for the network to be restored
    await ha_helpers.wait_until_unit_in_status(ops_test, primary, active_unit, "SECONDARY")

    await verify_endpoints(ops_test, mongodb_application_name)
