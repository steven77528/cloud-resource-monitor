from concurrent.futures import ThreadPoolExecutor
import ssl
from unittest.mock import Mock
import warnings

import pytest
from aliyunsdkcore.vendored.requests.packages.urllib3 import util
from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_ as sdk_ssl

from cloud_monitor.aliyun_compat import ensure_aliyun_tls_compatibility


@pytest.fixture
def broken_tls_detection(monkeypatch):
    monkeypatch.delattr(ssl, "wrap_socket", raising=False)
    monkeypatch.setattr(ssl, "HAS_SNI", True)
    monkeypatch.setattr(sdk_ssl, "HAS_SNI", False)
    monkeypatch.setattr(util, "HAS_SNI", False)
    monkeypatch.setattr(sdk_ssl, "PROTOCOL_SSLv23", None)
    monkeypatch.setattr(sdk_ssl, "CERT_NONE", ssl.CERT_NONE, raising=False)
    monkeypatch.delattr(sdk_ssl, "CERT_NONE")
    monkeypatch.setattr(sdk_ssl, "SSLContext", ssl.SSLContext)
    monkeypatch.setattr(sdk_ssl, "IS_PYOPENSSL", False)
    monkeypatch.setattr(sdk_ssl, "IS_SECURETRANSPORT", False)


def test_repair_sends_hostname_and_keeps_certificate_verification(broken_tls_detection):
    original_wrapper = sdk_ssl.ssl_wrap_socket
    original_context = sdk_ssl.SSLContext
    original_filter = list(warnings.filters)
    ensure_aliyun_tls_compatibility()
    context = Mock()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        sdk_ssl.ssl_wrap_socket(object(), server_hostname="cloud.example.test", ssl_context=context)
    assert context.wrap_socket.call_args.kwargs == {"server_hostname": "cloud.example.test"}
    assert not any(type(item.message).__name__ == "SNIMissingWarning" for item in captured)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert sdk_ssl.create_urllib3_context().verify_mode == ssl.CERT_REQUIRED
    assert sdk_ssl.ssl_wrap_socket is original_wrapper
    assert sdk_ssl.SSLContext is original_context
    assert sdk_ssl.CERT_NONE == ssl.CERT_NONE
    assert sdk_ssl.PROTOCOL_SSLv23 == ssl.PROTOCOL_TLS
    assert sdk_ssl.HAS_SNI is True and util.HAS_SNI is True
    assert warnings.filters == original_filter
    assert not hasattr(ssl, "wrap_socket")


@pytest.mark.parametrize("mode", ["older_python", "no_native_sni", "custom_context", "pyopenssl",
                                 "securetransport", "already_supported", "different_failure"])
def test_other_tls_implementations_and_states_are_untouched(broken_tls_detection, monkeypatch, mode):
    if mode == "older_python":
        monkeypatch.setattr(ssl, "wrap_socket", object(), raising=False)
    elif mode == "no_native_sni":
        monkeypatch.setattr(ssl, "HAS_SNI", False)
    elif mode == "custom_context":
        monkeypatch.setattr(sdk_ssl, "SSLContext", object())
    elif mode == "pyopenssl":
        monkeypatch.setattr(sdk_ssl, "IS_PYOPENSSL", True)
    elif mode == "securetransport":
        monkeypatch.setattr(sdk_ssl, "IS_SECURETRANSPORT", True)
    elif mode == "already_supported":
        monkeypatch.setattr(sdk_ssl, "HAS_SNI", True)
    else:
        monkeypatch.setattr(sdk_ssl, "PROTOCOL_SSLv23", ssl.PROTOCOL_TLS)
    expected = (sdk_ssl.HAS_SNI, sdk_ssl.PROTOCOL_SSLv23, util.HAS_SNI)
    ensure_aliyun_tls_compatibility()
    assert (sdk_ssl.HAS_SNI, sdk_ssl.PROTOCOL_SSLv23, util.HAS_SNI) == expected
    assert not hasattr(sdk_ssl, "CERT_NONE")


def test_repair_is_idempotent_under_concurrent_initialization(broken_tls_detection):
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: ensure_aliyun_tls_compatibility(), range(32)))
    assert sdk_ssl.HAS_SNI is True and util.HAS_SNI is True
    assert sdk_ssl.CERT_NONE == ssl.CERT_NONE
    assert sdk_ssl.PROTOCOL_SSLv23 == ssl.PROTOCOL_TLS
