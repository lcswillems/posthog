import argparse
import os
import shutil
import datetime
import re
import xml.etree.ElementTree as ET
from xml import etree

# For revertible cloud deploys:
# 1. Develop using the python files at the top level of `user_scripts`, with schema defined in `docker/clickhouse/user_defined_function.xml`
# 2. If you're made any changes to UDFs, when ready to deploy, increment the version below and run this file
# 3. Overwrite `user_defined_function.xml` in the `posthog-cloud-infra` repo (us, eu, and dev) with `user_scripts/latest_user_defined_function.xml` and deploy it
# 4. Land a version of the posthog repo with the updated `user_scripts` folder from the new branch (make sure this PR doesn't include changes to this file with the new version)
# 5. Run the `copy_udfs_to_clickhouse` action in the `posthog_cloud_infra` repo to deploy the `user_scripts` folder to clickhouse
# 6. After that deploy goes out, it is safe to land and deploy the full changes to the `posthog` repo
UDF_VERSION = 3  # Last modified by: @aspicer, 2024-10-30

# Clean up all versions less than this
EARLIEST_UDF_VERSION = 2

CLICKHOUSE_XML_FILENAME = "user_defined_function.xml"
ACTIVE_XML_CONFIG = "../../docker/clickhouse/user_defined_function.xml"

format_version_string = lambda version: f"v{version}"
VERSION_STR = format_version_string(UDF_VERSION)
LAST_VERSION_STR = format_version_string(UDF_VERSION - 1)

augment_function_name = lambda name: f"{name}_{VERSION_STR}"


def prepare_version(force=False):
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_scripts"))
    if args.force:
        shutil.rmtree(VERSION_STR)
    try:
        os.mkdir(VERSION_STR)
    except FileExistsError:
        if not args.force:
            raise FileExistsError(
                f"A directory already exists for this version at posthog/user_scripts/{VERSION_STR}. Did you forget to increment the version? If not, delete the folder and run this again, or run this script with a -f"
            )
    for file in os.listdir():
        if os.path.isfile(file) and not file.endswith(".xml"):
            shutil.copy(file, VERSION_STR)

    base_xml = ET.parse(ACTIVE_XML_CONFIG)

    if os.path.exists(LAST_VERSION_STR):
        last_version_xml = ET.parse(os.path.join(LAST_VERSION_STR, CLICKHOUSE_XML_FILENAME))
    else:
        last_version_xml = ET.parse(ACTIVE_XML_CONFIG)

    last_version_root = last_version_xml.getroot()

    # Remove old versions from last_version
    for function in list(last_version_root):
        name = function.find("name")
        match = re.search(r"_v(\d+)$", name.text)
        if match is None or int(match.group(1)) < EARLIEST_UDF_VERSION:
            last_version_root.remove(function)

    # We want to update the name and the command to include the version, and add it to last version
    for function in list(base_xml.getroot()):
        name = function.find("name")
        name.text = augment_function_name(name.text)
        command = function.find("command")
        command.text = f"{VERSION_STR}/{command.text}"
        last_version_root.append(function)

    comment = etree.ElementTree.Comment(
        f" Version: {VERSION_STR}. Generated at: {datetime.datetime.now(datetime.UTC).isoformat()}\nThis file is autogenerated by udf_versioner.py. Do not edit this, only edit the version at docker/clickhouse/user_defined_function.xml"
    )
    last_version_root.insert(0, comment)

    last_version_xml.write(os.path.join(VERSION_STR, CLICKHOUSE_XML_FILENAME))
    last_version_xml.write(f"latest_{CLICKHOUSE_XML_FILENAME}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a new version for UDF deployment.")
    parser.add_argument("-f", "--force", action="store_true", help="override existing directories")
    args = parser.parse_args()

    prepare_version(args.force)
