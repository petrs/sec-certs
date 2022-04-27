import json
from pathlib import Path

import pytest
from jsondiff import diff
from sec_certs.sample.common_criteria import CommonCriteriaCert

from sec_certs_page.common.objformats import ObjFormat, WorkingFormat


@pytest.fixture(scope="module")
def cert1():
    test_path = Path(__file__).parent / "data" / "cert1.json"
    with test_path.open() as f:
        return test_path, json.load(f)


@pytest.fixture(scope="module")
def cert2():
    test_path = Path(__file__).parent / "data" / "cert2.json"
    with test_path.open() as f:
        return test_path, json.load(f)


def test_load_cert(cert1):
    test_path, cert_data = cert1
    cert = CommonCriteriaCert.from_json(test_path)
    storage_format = ObjFormat(cert).to_raw_format().to_working_format().to_storage_format()
    obj_format = storage_format.to_working_format().to_raw_format().to_obj_format()
    assert cert == obj_format.get()

    json_mapping = storage_format.to_json_mapping()
    assert json_mapping == cert_data


def test_diff(cert1, cert2):
    d = diff(cert1[1], cert2[1], syntax="explicit")
    working = WorkingFormat(d)
    working.to_storage_format().get()
    working.to_raw_format().get()
    working_back = working.to_storage_format().to_working_format().get()
    assert working.get() == working_back