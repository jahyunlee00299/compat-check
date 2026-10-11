"""Wheel-vs-source-build classification (offline: PyPI is injected or mocked).

The release file lists mirror measured PyPI data from 2026-10-11 (Python
3.15.0rc3 on linux x86_64): PyYAML 6.0.3 ships cp38..cp314 wheels only,
hdbscan 0.8.44 cp310..cp314, thermosteam is sdist-only. All of them RESOLVE
under `uv pip install --dry-run` and used to be reported as plain OK.
"""
import sys
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from compat_check import wheels
from compat_check.wheels import (
    Target,
    WheelFetchError,
    check_wheels,
    format_report,
    parse_wheel_filename,
    wheel_is_compatible,
)

LINUX = Target(python=(3, 15), system="linux", machine="x86_64", glibc=(2, 39))
LINUX_OLD_GLIBC = Target(python=(3, 15), system="linux", machine="x86_64", glibc=(2, 17))
MUSL = Target(python=(3, 15), system="linux", machine="x86_64", musl=True)
WIN = Target(python=(3, 15), system="windows", machine="x86_64")
MAC_ARM = Target(python=(3, 15), system="macos", machine="arm64", macos=(14, 0))


def _wheel(filename):
    return {"filename": filename, "packagetype": "bdist_wheel", "yanked": False}


def _sdist(filename):
    return {"filename": filename, "packagetype": "sdist", "yanked": False}


def test_parse_wheel_filename_handles_compound_tags_and_build_numbers():
    t = parse_wheel_filename("numpy-2.0.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    assert t.python == ("cp312",) and t.abi == ("cp312",)
    assert t.platform == ("manylinux_2_17_x86_64", "manylinux2014_x86_64")
    assert parse_wheel_filename("six-1.16.0-1-py2.py3-none-any.whl").python == ("py2", "py3")
    assert parse_wheel_filename("not-a-wheel.tar.gz") is None
    assert parse_wheel_filename("bad-name.whl") is None


def test_pure_python_wheels_are_compatible():
    assert wheel_is_compatible("six-1.16.0-py2.py3-none-any.whl", LINUX)
    assert wheel_is_compatible("rich-13.0-py3-none-any.whl", LINUX)
    assert wheel_is_compatible("old-1.0-py38-none-any.whl", LINUX)
    assert not wheel_is_compatible("py2only-1.0-py2-none-any.whl", LINUX)


def test_exact_cpython_wheel_must_match_the_target_minor():
    assert wheel_is_compatible("x-1-cp315-cp315-manylinux_2_17_x86_64.whl", LINUX)
    assert not wheel_is_compatible("x-1-cp314-cp314-manylinux_2_17_x86_64.whl", LINUX)
    assert not wheel_is_compatible("x-1-cp316-cp316-manylinux_2_17_x86_64.whl", LINUX)


def test_abi3_wheel_works_on_that_python_and_newer_only():
    assert wheel_is_compatible("x-1-cp38-abi3-manylinux_2_17_x86_64.whl", LINUX)
    assert wheel_is_compatible("x-1-cp315-abi3-manylinux_2_17_x86_64.whl", LINUX)
    assert not wheel_is_compatible("x-1-cp316-abi3-manylinux_2_17_x86_64.whl", LINUX)


def test_free_threaded_wheels_are_a_different_abi():
    assert not wheel_is_compatible("x-1-cp315t-cp315t-manylinux_2_17_x86_64.whl", LINUX)
    assert not wheel_is_compatible("x-1-cp313t-cp313t-manylinux_2_17_x86_64.whl",
                                    Target(python=(3, 13), system="linux", machine="x86_64"))


def test_platform_matching_per_system():
    f = "x-1-cp315-cp315-{}.whl"
    assert wheel_is_compatible(f.format("manylinux2014_x86_64"), LINUX)
    assert not wheel_is_compatible(f.format("manylinux2014_aarch64"), LINUX)
    assert not wheel_is_compatible(f.format("win_amd64"), LINUX)
    assert wheel_is_compatible(f.format("win_amd64"), WIN)
    assert not wheel_is_compatible(f.format("win_arm64"), WIN)
    assert wheel_is_compatible(f.format("macosx_11_0_arm64"), MAC_ARM)
    assert wheel_is_compatible(f.format("macosx_10_9_universal2"), MAC_ARM)
    assert not wheel_is_compatible(f.format("macosx_11_0_x86_64"), MAC_ARM)
    assert not wheel_is_compatible(f.format("macosx_15_0_arm64"), MAC_ARM)  # newer than host


def test_manylinux_respects_host_glibc_and_musl():
    f = "x-1-cp315-cp315-{}.whl"
    assert wheel_is_compatible(f.format("manylinux_2_28_x86_64"), LINUX)
    assert not wheel_is_compatible(f.format("manylinux_2_28_x86_64"), LINUX_OLD_GLIBC)
    assert wheel_is_compatible(f.format("manylinux2014_x86_64"), LINUX_OLD_GLIBC)
    assert not wheel_is_compatible(f.format("manylinux2014_x86_64"), MUSL)
    assert wheel_is_compatible(f.format("musllinux_1_2_x86_64"), MUSL)
    assert not wheel_is_compatible(f.format("musllinux_1_2_x86_64"), LINUX)


def test_unknown_glibc_accepts_any_manylinux_of_the_right_arch():
    unknown = Target(python=(3, 15), system="linux", machine="x86_64")
    assert wheel_is_compatible("x-1-cp315-cp315-manylinux_2_34_x86_64.whl", unknown)


def _fetch_from(table):
    def fetch(name, version):
        key = f"{name}=={version}"
        if key not in table:
            raise WheelFetchError(f"{key} is not on PyPI (HTTP 404)")
        return table[key]
    return fetch


_PYYAML = [
    _wheel("pyyaml-6.0.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"),
    _wheel("pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"),
    _wheel("pyyaml-6.0.3-cp314-cp314-win_amd64.whl"),
    _sdist("pyyaml-6.0.3.tar.gz"),
]


def test_pyyaml_like_release_needs_a_source_build_on_315():
    report = check_wheels(["PyYAML==6.0.3"], LINUX, fetch=_fetch_from({"PyYAML==6.0.3": _PYYAML}))
    (finding,) = report.findings
    assert finding.status == "source"
    assert "cp314-cp314" in finding.detail and "cp313-cp313" in finding.detail
    assert report.needs_attention


def test_same_release_is_fine_on_314():
    py314 = Target(python=(3, 14), system="linux", machine="x86_64", glibc=(2, 39))
    report = check_wheels(["PyYAML==6.0.3"], py314, fetch=_fetch_from({"PyYAML==6.0.3": _PYYAML}))
    assert report.findings[0].status == "wheel"
    assert not report.needs_attention


def test_sdist_only_release_is_a_source_build():
    report = check_wheels(["thermosteam==0.5.2"], LINUX, fetch=_fetch_from(
        {"thermosteam==0.5.2": [_sdist("thermosteam-0.5.2.tar.gz")]}))
    assert report.findings[0].status == "source"
    assert "sdist only" in report.findings[0].detail


def test_wheels_only_for_other_platforms_are_said_so():
    report = check_wheels(["winonly==1.0"], LINUX, fetch=_fetch_from(
        {"winonly==1.0": [_wheel("winonly-1.0-cp315-cp315-win_amd64.whl")]}))
    assert report.findings[0].status == "source"
    assert "other platforms" in report.findings[0].detail and "win_amd64" in report.findings[0].detail


def test_yanked_wheels_do_not_count():
    yanked = {**_wheel("x-1.0-py3-none-any.whl"), "yanked": True}
    report = check_wheels(["x==1.0"], LINUX, fetch=_fetch_from({"x==1.0": [yanked, _sdist("x-1.0.tar.gz")]}))
    assert report.findings[0].status == "source"


def test_git_and_url_entries_are_reported_as_non_pypi_not_checked():
    calls = []

    def fetch(name, version):
        calls.append(name)
        return []

    report = check_wheels(
        ["tool @ git+https://github.com/o/r@abc123", "local @ file:///tmp/local"],
        LINUX, fetch=fetch)
    assert [f.status for f in report.findings] == ["non-pypi", "non-pypi"]
    assert calls == [], "a non-PyPI source must not be looked up on PyPI"
    assert not report.needs_attention  # reported, but not a defect


def test_fetch_failure_is_unknown_never_silently_ok():
    report = check_wheels(["ghost==1.0"], LINUX, fetch=_fetch_from({}))
    (finding,) = report.findings
    assert finding.status == "unknown"
    assert "404" in finding.detail
    assert report.needs_attention


def test_real_fetch_surfaces_network_errors_as_wheelfetcherror():
    with mock.patch.object(wheels.urllib.request, "urlopen",
                            side_effect=urllib.error.URLError("temporary failure")):
        try:
            wheels.fetch_release_files("anything", "1.0")
        except WheelFetchError as e:
            assert "network error" in str(e) and "temporary failure" in str(e)
        else:
            raise AssertionError("a network failure must not return an empty file list")


def test_real_fetch_retries_once_on_a_transient_failure():
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = b'{"urls": [{"filename": "a-1-py3-none-any.whl"}]}'
    with mock.patch.object(wheels.urllib.request, "urlopen",
                            side_effect=[urllib.error.URLError("blip"), ok]) as op:
        files = wheels.fetch_release_files("a", "1")
    assert op.call_count == 2 and files[0]["filename"] == "a-1-py3-none-any.whl"


def test_http_404_is_not_retried_and_is_not_confused_with_no_wheels():
    err = urllib.error.HTTPError("u", 404, "nf", {}, None)
    with mock.patch.object(wheels.urllib.request, "urlopen", side_effect=err) as op:
        try:
            wheels.fetch_release_files("a", "1")
        except WheelFetchError as e:
            assert "not on PyPI" in str(e)
        else:
            raise AssertionError("expected WheelFetchError")
    assert op.call_count == 1


def test_report_preserves_input_order_and_format_names_every_section():
    table = {"PyYAML==6.0.3": _PYYAML,
             "six==1.16.0": [_wheel("six-1.16.0-py2.py3-none-any.whl")]}
    report = check_wheels(
        ["six==1.16.0", "PyYAML==6.0.3", "ghost==1.0", "tool @ git+https://x/y"],
        LINUX, fetch=_fetch_from(table))
    assert [f.spec for f in report.findings] == [
        "six==1.16.0", "PyYAML==6.0.3", "ghost==1.0", "tool @ git+https://x/y"]
    text = "\n".join(format_report(report))
    assert "WARN — 1 package(s) have no wheel for Python 3.15 on linux x86_64" in text
    assert "install will build from source" in text
    assert "! PyYAML==6.0.3" in text
    assert "WARN — wheel status UNKNOWN for 1 package(s)" in text
    assert "? ghost==1.0" in text
    assert "~ tool @ git+https://x/y" in text


def test_clean_report_says_how_many_were_checked():
    report = check_wheels(["six==1.16.0"], LINUX, fetch=_fetch_from(
        {"six==1.16.0": [_wheel("six-1.16.0-py2.py3-none-any.whl")]}))
    assert format_report(report) == [
        "wheels: all 1 PyPI package(s) have a wheel for Python 3.15 on linux x86_64 (glibc 2.39)"]


def test_empty_input_makes_no_requests():
    report = check_wheels([], LINUX, fetch=lambda n, v: (_ for _ in ()).throw(AssertionError("fetched")))
    assert report.findings == []


def test_host_target_reads_this_machine():
    t = wheels.host_target("3.15.0")
    assert t.python == (3, 15)
    assert t.system in ("linux", "windows", "macos") or t.system == sys.platform


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nall {len(tests)} wheel tests passed")
