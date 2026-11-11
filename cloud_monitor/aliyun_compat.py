"""Compatibility for the TLS feature detection in aliyun-python-sdk-core 2.16.0."""

import ssl
import threading


_TLS_LOCK = threading.Lock()


def ensure_aliyun_tls_compatibility() -> None:
    if hasattr(ssl, "wrap_socket") or not ssl.HAS_SNI:
        return

    from aliyunsdkcore.vendored.requests.packages.urllib3 import util
    from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_ as sdk_ssl

    with _TLS_LOCK:
        if (sdk_ssl.SSLContext is not ssl.SSLContext or sdk_ssl.IS_PYOPENSSL
                or sdk_ssl.IS_SECURETRANSPORT or sdk_ssl.HAS_SNI
                or sdk_ssl.PROTOCOL_SSLv23 is not None):
            return
        # Python 3.12 removed ssl.wrap_socket. Its failed import skips all three
        # constants in the SDK's bundled urllib3, despite native SSLContext support.
        sdk_ssl.CERT_NONE = ssl.CERT_NONE
        sdk_ssl.PROTOCOL_SSLv23 = ssl.PROTOCOL_TLS
        sdk_ssl.HAS_SNI = ssl.HAS_SNI
        util.HAS_SNI = ssl.HAS_SNI
