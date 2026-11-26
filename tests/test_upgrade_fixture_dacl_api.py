"""The QA adapter sets only DACL information; native roundtrips run on Windows."""

import ctypes
import importlib.util
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/acceptance/upgrade_rehearsal.py"
SPEC = importlib.util.spec_from_file_location("qa_dacl_api_test", SOURCE)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


def adapter(*, protected, null_dacl=False):
    instance = object.__new__(qa._WindowsDacl)
    instance.c, instance.w = ctypes, wintypes
    expected = {
        "sddl": "D:PAI" if protected else "D:AI",
        "protected": protected,
        "dacl_present": True,
        "null_dacl": null_dacl,
        "dacl_sha256": None if null_dacl else "all-original-aces",
        "explicit_ace_sha256": [] if null_dacl else ["first-explicit", "second-explicit"],
    }
    written, freed = [], []

    def dacl(_security, present, output, defaulted):
        present._obj.value = 1
        output._obj.value = None if null_dacl else 1234
        defaulted._obj.value = 0
        return 1

    def write(*args):
        written.append(args)
        return 0

    instance.a = SimpleNamespace(GetSecurityDescriptorDacl=dacl, SetNamedSecurityInfoW=write)
    instance.k = SimpleNamespace(LocalFree=freed.append)
    instance.validate = lambda value: None
    instance._decode = lambda value: ctypes.c_void_p(5678)
    instance.read = lambda path: dict(expected)
    return instance, expected, written, freed


@pytest.mark.parametrize("protected", [True, False])
@pytest.mark.parametrize("null_dacl", [True, False])
def test_only_dacl_and_the_source_protection_bit_are_written(tmp_path, protected, null_dacl):
    api, entry, written, freed = adapter(protected=protected, null_dacl=null_dacl)
    api.apply(tmp_path, entry)
    assert len(written) == 1
    path, object_type, information, owner, group, dacl, sacl = written[0]
    assert path == qa.long_path(tmp_path) and object_type == 1
    assert information == 4 | (0x80000000 if protected else 0x20000000)
    assert owner is group is sacl is None
    assert dacl.value == (None if null_dacl else 1234)
    assert [p.value for p in freed] == [5678]


def test_unprotected_policy_accepts_new_parent_inheritance_but_not_explicit_ace_changes(tmp_path):
    api, entry, _, _ = adapter(protected=False)
    api.read = lambda path: {**entry, "dacl_sha256": "new-inherited-aces"}
    api.apply(tmp_path, entry)
    api.read = lambda path: {**entry, "explicit_ace_sha256": entry["explicit_ace_sha256"][::-1]}
    with pytest.raises(ValueError, match="readback mismatch"):
        api.apply(tmp_path, entry)


def test_protected_policy_ignores_ai_bookkeeping_but_requires_exact_aces(tmp_path):
    api, entry, _, _ = adapter(protected=True)
    api.read = lambda path: {**entry, "sddl": "D:P"}
    api.apply(tmp_path, entry)
    api.read = lambda path: {**entry, "dacl_sha256": "different-aces"}
    with pytest.raises(ValueError, match="readback mismatch"):
        api.apply(tmp_path, entry)


def test_null_and_empty_dacl_are_not_conflated_on_readback(tmp_path):
    api, entry, _, _ = adapter(protected=False, null_dacl=True)
    api.read = lambda path: {**entry, "null_dacl": False}
    with pytest.raises(ValueError, match="readback mismatch"):
        api.apply(tmp_path, entry)


def test_forged_protected_metadata_fails_before_any_security_write():
    api, entry, written, freed = adapter(protected=True)
    api._describe = lambda security: {
        key: value for key, value in entry.items() if key != "sddl"
    }
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        qa._WindowsDacl.validate(api, {**entry, "protected": False})
    assert written == [] and len(freed) == 1


def test_unsupported_owner_descriptor_is_not_decoded_or_written():
    api, _, written, _ = adapter(protected=True)
    with pytest.raises(ValueError, match="DACL-only"):
        qa._WindowsDacl._decode(api, {"sddl": "O:SYG:SY"})
    assert written == []
