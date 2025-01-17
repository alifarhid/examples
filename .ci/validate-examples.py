"""
Examples in the `examples/` directory must contain valid recipes

This script validates the content of those directories,
to try to prevent pushing broken code to customer's
Saturn installations.
"""
import argparse
import glob
import json
import os
import re
import requests
import subprocess
import sys

from jsonschema import validate
from jsonschema.exceptions import ValidationError
from ruamel import yaml
from typing import Dict, List, Optional


parser = argparse.ArgumentParser()
parser.add_argument("--examples-dir", type=str, help="Path to the 'examples' directory to check")

parser.add_argument(
    "--skip-image-check",
    action="store_true",
    help="Whether to skip the image check (for local use)",
)

ADMIN_DIRS = ["_img"]
ARGS = parser.parse_args()
DIRECTORY_REGEX = r"^[0-9a-z\-]+$"
FILENAME_REGEX = r"^[0-9A-Za-z\-\.\_]+$"
SATURN_DIR_NAME = ".saturn"
SATURN_JSON_NAME = "saturn.json"
TEMPLATES_JSON_NAMES = ["templates-hosted.json", "templates-enterprise.json"]
EXAMPLES_DIR = ARGS.examples_dir
SKIP_IMAGE_CHECK = ARGS.skip_image_check

# This points to a json file in the saturncloud/recipe repo.
# The BASE_URL is there just to ensure the URL fits on one line and doesn't break the link validator
with open("RECIPE_SCHEMA_VERSION", "r") as f:
    RECIPE_SCHEMA_VERSION = f.read()
RECIPE_SCHEMA_BASE_URL = "raw.githubusercontent.com/saturncloud/recipes"
RECIPE_SCHEMA_URL = (
    f"https://{RECIPE_SCHEMA_BASE_URL}/{RECIPE_SCHEMA_VERSION}/resources/schema.json"
)
INSTANCE_TYPE_OPTIONS_URL = "https://saturncloud.io/static/constants.yaml"


class ErrorCollection:

    _errors: List[str] = []

    def add(self, error: str):
        self._errors.append(error)

    @property
    def num_errors(self) -> int:
        return len(self._errors)

    def report(self) -> None:
        print("\n------ check results ------\n")
        print(f"{self.num_errors} errors found checking examples\n")
        i = 1
        for error in self._errors:
            print(f"{i}. {error}")
            i += 1
        sys.exit(self.num_errors)


def validate_recipe(schema, instance_type_options, recipe_path, example_dir):
    """Assuming that 'recipe-schema.json' is available, validate recipe file"""

    with open(recipe_path, "r") as f:
        recipe = json.load(f)

    validate(instance=recipe, schema=schema)

    name_pre = recipe["name"]
    name_prefix = "example-"
    if not name_pre.startswith(name_prefix):
        raise ValidationError(f"name ('{name_pre}') needs to start with {name_prefix}")

    image_uri = recipe["image_uri"]
    image_name, image_tag = image_uri.split(":")
    if not SKIP_IMAGE_CHECK:
        image_pieces = image_name.rsplit("/", 2)
        image_registry, image_repository = (
            (image_pieces[0], f"{image_pieces[1]}/{image_pieces[2]}")
            if len(image_pieces) == 3
            else (None, image_name)
        )
        image_exists = image_exists_on_registry(
            registry=image_registry, image_name=image_repository, image_tag=image_tag
        )
        if not image_exists:
            raise ValidationError(
                f"image '{image_name}:{image_tag}' is not available on Docker Hub."
            )

    working_dir = recipe["working_directory"]
    working_dir_prefix = "/home/jovyan/examples/examples/"
    if not working_dir.startswith(working_dir_prefix):
        raise ValidationError(
            f"working_directory ('{working_dir}') needs to start with {working_dir_prefix}"
        )
    working_dir_suffix = working_dir.replace(working_dir_prefix, "")
    if not working_dir_suffix == example_dir:
        raise ValidationError(
            f"working_directory ('{working_dir}') needs to end with "
            f"the name of the example directory: {example_dir}"
        )

    rel_path = working_dir.replace(working_dir_prefix, "")
    abs_path = os.path.join(EXAMPLES_DIR, rel_path)
    if not os.path.exists(abs_path):
        raise ValidationError(f"working_directory ('{working_dir}') needs to point to a real path")

    git_repositories = recipe["git_repositories"]
    if len(git_repositories) < 1:
        raise ValidationError("git_repositories cannot be empty")
    if not all("path" in repo for repo in git_repositories):
        raise ValidationError(f"At least one git_repository is missing a path {git_repositories}")

    jupyter = recipe.get("jupyter_server")
    deployment = recipe.get("deployment")
    job = recipe.get("job")
    rstudio = recipe.get("rstudio_server")
    resource = jupyter or deployment or job or rstudio
    workspace = jupyter or rstudio

    if resource["instance_type"] not in instance_type_options:
        raise ValidationError(
            f"instance_type ('{resource['instance_type']}') is not a valid option. "
            f"Look at {INSTANCE_TYPE_OPTIONS_URL} for valid options."
        )

    if recipe.get("dask_cluster", None):
        if recipe["dask_cluster"]["num_workers"] > 3:
            raise ValidationError("there should not be more than 3 workers per dask cluster.")

        if recipe["dask_cluster"]["worker"]["instance_type"] != resource["instance_type"]:
            raise ValidationError(
                "Dask worker instance type should match the instance type "
                "of its attached jupyter_server or deployment"
            )

    if workspace is not None and "disk_space" not in workspace:
        raise ValidationError("disk_space is required for workspaces.")


def image_exists_on_registry(registry: Optional[str], image_name: str, image_tag: str) -> bool:
    """
    Given an image name and image_tag, check if it is
    publicly-accessible on DockerHub.

    Based on the code from this blog post:
    * https://ops.tips/blog/inspecting-docker-image-without-pull/

    :param image_name: Name of an image, such as ``continuumio/miniconda3``
    :param image_tag: Tag for the image, such as ``4.8.2``

    :Example:

    .. code-block:: python

        # should be True
        image_exists_on_registry("continuumio/miniconda3", "4.8.2")

        # should be False
        import uuid
        image_exists_on_registry(str(uuid.uuid4()), "0.0.1")
    """
    headers: Dict[str, str] = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json",
    }
    if registry is None or registry == "docker.io":
        # docker hub requires anonymous login
        url = (
            "https://auth.docker.io/token?scope=repository:"
            f"{image_name}:pull&service=registry.docker.io"
        )
        res = requests.get(url=url)
        res.raise_for_status()
        token = res.json()["token"]
        headers["Authorization"] = f"Bearer {token}"
        registry = "registry-1.docker.io"
    elif registry == "public.ecr.aws":
        # ECR public does not support anonymous auth
        # get a token via CLI
        token = subprocess.run(
            [
                "aws",
                "ecr-public",
                "get-authorization-token",
                "--region=us-east-1",
                "--output=text",
                "--query",
                "authorizationData.authorizationToken",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        headers["Authorization"] = f"Bearer {token}"
    res = requests.head(
        url=f"https://{registry}/v2/{image_name}/manifests/{image_tag}",
        headers=headers,
    )

    return res.status_code == 200


def _lint_python_cell(file_name: str, code_lines: List[str]) -> List[str]:
    """
    Given the content of a Python code cell, check it for problems we
    want to avoid.

    :param file_name: Name of the notebook this code came from. This is used
        to make error messages more informative.
    :param code_lines: List of strings, where each item in the list is one
        line of Python code.

    :return: A list of strings, where each string represents an error
        found by this check
    """
    errors = []

    # Jupyter notebooks already store literal newlines
    code_str = "".join(code_lines)

    WARNING_FILTER_REGEX = r".*warnings\.filter.*"
    if bool(re.search(WARNING_FILTER_REGEX, code_str)):
        msg = (
            f"Found use of warnings.simplefilter() or warnings.filterwarnings() in {file_name}. "
            "Do not filter out warnings in example notebooks. Try to fix them or "
            "add text explaining why they can be safely ignored."
        )
        errors.append(msg)

    DELAYED_DECORATOR_REGEX = r"@delayed"
    if bool(re.search(DELAYED_DECORATOR_REGEX, code_str)):
        msg = (
            f"Found a use of '@delayed' in {file_name}. "
            "Instead, 'import dask' and then use '@dask.delayed'."
        )
        errors.append(msg)

    return errors


if __name__ == "__main__":

    ERRORS = ErrorCollection()

    res = requests.get(url=RECIPE_SCHEMA_URL)
    schema = res.json()

    res = requests.get(url=INSTANCE_TYPE_OPTIONS_URL)
    instance_type_options = yaml.safe_load(res.text)["tiers"]

    example_dirs = os.listdir(EXAMPLES_DIR)
    if len(example_dirs) == 0:
        ERRORS.add(f"No directories found under '{EXAMPLES_DIR}'")

    for template_name in TEMPLATES_JSON_NAMES:
        templates_json = os.path.join(EXAMPLES_DIR, "..", ".saturn", template_name)
        with open(templates_json, "r") as f:
            templates = json.load(f)["templates"]

        weights = {}

        print(f"Working on {template_name}")
        for template in templates:
            title = template["title"]
            weight = template["weight"]
            thumbnail = template["thumbnail_image_url"]

            res = requests.get(url=thumbnail)
            if not res.status_code == 200:
                msg = f"Thumbnail image ({thumbnail}) for {title} is not a valid url."

            matches = [key for key, value in weights.items() if weight == value]
            if matches:
                msg = f"Weight ({weight}) for '{title}' is non-unique. It matches {matches}."
                ERRORS.add(msg)
            weights[title] = weight

            example_dir = template["recipe_path"].split("examples/")[-1].split("/")[0]
            if example_dir not in example_dirs:
                msg = (
                    f"Example directory: '{example_dir}' referenced by template: '{title}' does "
                    "not exist."
                )
                ERRORS.add(msg)

    for example_dir in example_dirs:
        print(f"Working on directory '{example_dir}'")
        full_dir = os.path.join(EXAMPLES_DIR, example_dir)

        if not bool(re.search(DIRECTORY_REGEX, os.path.basename(example_dir))):
            msg = (
                f"All directories under '{EXAMPLES_DIR}' should be named with only "
                "lower alphanumeric characters and dashes. "
                f"'{full_dir}` violates this rule."
            )
            ERRORS.add(msg)
            continue

        all_files = glob.glob(f"{full_dir}/**", recursive=True)
        if len(all_files) == 0:
            msg = f"Directory '{full_dir}' is empty"
            ERRORS.add(msg)
            continue

        first_level_dirs = glob.glob(f"{full_dir}/**", recursive=False)
        for fname in first_level_dirs:
            if os.path.isdir(fname):
                readme_file = os.path.join(fname, "README.md")
                if not os.path.isfile(readme_file):
                    msg = (
                        f"Every directory two-levels below '{EXAMPLES_DIR}' "
                        f"must have a README.md. None found for '{fname}'."
                    )
                    ERRORS.add(msg)

        # For all the notebooks (and there may be none), check that all cells in that
        # notebook should be cleared of output and execution counts
        notebook_files = [f for f in all_files if f.endswith(".ipynb")]
        for notebook_file in notebook_files:
            with open(notebook_file, "r") as f:
                notebook_dict = json.loads(f.read())
            non_empty_cells = 0
            for cell in notebook_dict["cells"]:
                if cell.get("outputs", []) or cell.get("execution_count", None):
                    non_empty_cells += 1
                if cell["cell_type"] == "code":
                    linting_errors = _lint_python_cell(notebook_file, cell["source"])
                    for err in linting_errors:
                        ERRORS.add(err)
            if non_empty_cells > 0:
                msg = (
                    f"Found {non_empty_cells} non-empty cells in '{notebook_file}'. "
                    "Clear all outputs and re-commit this file."
                )
                ERRORS.add(msg)

        # all files and directories should have appropriate name
        for fname in all_files:
            if os.path.isdir(fname):
                base_name = os.path.basename(os.path.normpath(fname))
                is_valid = bool(re.search(DIRECTORY_REGEX, base_name))
                msg = (
                    f"[here] All directories should be named with only "
                    "lower alphanumeric characters and dashes. "
                    f"'{fname}` violates this rule."
                )
                if base_name in ADMIN_DIRS:
                    is_valid = True
            else:
                base_name = os.path.basename(fname)
                is_valid = bool(re.search(FILENAME_REGEX, base_name))
                msg = (
                    f"All files should be named with only "
                    "alphanumeric characters, dashes, underscores, and periods. "
                    f"'{fname}` violates this rule."
                )
            if not is_valid:
                ERRORS.add(msg)

        # each example must have a README.md
        readme_file = os.path.join(full_dir, "README.md")
        if not os.path.isfile(readme_file):
            msg = f"Every example must have a README.md. '{full_dir}' does not."
            ERRORS.add(msg)

        # each example must have a .saturn/ folder
        saturn_dir = os.path.join(full_dir, SATURN_DIR_NAME)
        if not os.path.isdir(saturn_dir):
            msg = f"'{full_dir}' does not include a '{SATURN_DIR_NAME}/' directory"
            ERRORS.add(msg)

        saturn_json = os.path.join(saturn_dir, SATURN_JSON_NAME)
        if not os.path.isfile(saturn_json):
            msg = f"Did not find saturn.json in '{saturn_dir}'. This file is required."
            ERRORS.add(msg)
            continue

        try:
            validate_recipe(schema, instance_type_options, saturn_json, example_dir)
        except ValidationError as e:
            msg = f"'{saturn_json}' has the following schema issues: {str(e)}"
            ERRORS.add(msg)
            continue

    ERRORS.report()
