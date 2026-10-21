import pytest

from app.handlers.start import split_cabinet_start_payload


@pytest.mark.parametrize(
    "payload, expected",
    [
        (None, (False, None)),
        ("", (False, "")),
        ("cabinet", (True, None)),
        ("cabinet_", (True, None)),
        ("cabinet_refmRHE9BMW", (True, "refmRHE9BMW")),
        ("refmRHE9BMW", (False, "refmRHE9BMW")),
        ("cabinetX", (False, "cabinetX")),
    ],
)
def test_split_cabinet_start_payload(payload, expected):
    assert split_cabinet_start_payload(payload) == expected
