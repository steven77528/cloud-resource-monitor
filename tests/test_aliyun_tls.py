"""Exercise the real SDK HTTPS path with local certificates, without cloud access."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
from threading import Thread
import warnings

from aliyunsdkcore.acs_exception.exceptions import ClientException
from aliyunsdkcore.vendored.requests.packages.urllib3 import util as sdk_util
from aliyunsdkcore.vendored.requests.packages.urllib3.exceptions import SNIMissingWarning
from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_ as sdk_ssl
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from cloud_monitor.providers.aliyun import AliyunCollector


HOSTNAME = "aliyun-tls.test"
WRONG_HOSTNAME = "wrong-host.test"


def _certificate(subject, issuer, public_key, signer, *, ca=False, hostname=None):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    )
    if hostname:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False,
        ).add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
    return builder.sign(signer, hashes.SHA256())


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    directory = tmp_path_factory.mktemp("aliyun-local-tls")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local TLS test CA")])
    ca = _certificate(ca_name, ca_name, ca_key.public_key(), ca_key, ca=True)
    unrelated_key = ec.generate_private_key(ec.SECP256R1())
    unrelated_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unrelated test CA")])
    unrelated_ca = _certificate(
        unrelated_name, unrelated_name, unrelated_key.public_key(), unrelated_key, ca=True,
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)])
    server_cert = _certificate(
        server_name, ca_name, server_key.public_key(), ca_key, hostname=HOSTNAME,
    )
    paths = {name: directory / name for name in ("ca.pem", "unrelated-ca.pem", "server.pem", "key.pem")}
    paths["ca.pem"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths["unrelated-ca.pem"].write_bytes(unrelated_ca.public_bytes(serialization.Encoding.PEM))
    paths["server.pem"].write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    paths["key.pem"].write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    return paths


@pytest.fixture
def tls_endpoint(monkeypatch, tls_material):
    if not hasattr(ssl, "wrap_socket") and ssl.HAS_SNI:
        # Simulate a fresh SDK import so earlier tests cannot mask missing RPC initialization.
        monkeypatch.setattr(sdk_ssl, "HAS_SNI", False)
        monkeypatch.setattr(sdk_ssl, "PROTOCOL_SSLv23", None)
        monkeypatch.setattr(sdk_util, "HAS_SNI", False)
        monkeypatch.setattr(sdk_ssl, "CERT_NONE", ssl.CERT_NONE, raising=False)
        monkeypatch.delattr(sdk_ssl, "CERT_NONE")
    names = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.headers.get("Host"))
            body = b'{"Success":true,"Code":"OK","Data":{"local_tls_test":true}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls_material["server.pem"], tls_material["key.pem"])
    context.set_servername_callback(lambda sock, name, ctx: names.append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    # All SDK requests must resolve to this local server, including failure cases.
    resolve = socket.getaddrinfo

    def local_only(host, port, *args, **kwargs):
        if host not in {HOSTNAME, WRONG_HOSTNAME} or int(port) != server.server_port:
            raise AssertionError(f"Unexpected network destination in TLS test: {host}:{port}")
        return resolve("127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "DEBUG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("ALIBABA_CLOUD_CA_BUNDLE", str(tls_material["ca.pem"]))
    collectors = []

    def request(hostname=HOSTNAME):
        collector = AliyunCollector({
            "id": "local-tls-test", "access_key_id": "test-key", "access_key_secret": "test-secret",
            "request_timeout_seconds": 2, "endpoints": {"dysmsapi": f"{hostname}:{server.server_port}"},
        })
        collectors.append(collector)
        return collector._rpc("dysmsapi", "QuerySendStatistics", "2017-05-25", {})

    try:
        yield request, names, requests
    finally:
        for collector in collectors:
            for client in collector._clients.values():
                client.session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_rpc_sends_sni_and_accepts_trusted_matching_certificate(tls_endpoint):
    request, names, requests = tls_endpoint
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        response = request()
    assert response["Data"]["local_tls_test"] is True
    assert names == [HOSTNAME]
    assert len(requests) == 1
    assert not any(issubclass(warning.category, SNIMissingWarning) for warning in recorded)


def test_rpc_rejects_certificate_for_wrong_hostname(tls_endpoint):
    request, names, requests = tls_endpoint
    with pytest.raises(ClientException) as caught:
        request(WRONG_HOSTNAME)
    assert caught.value.get_error_code() == "SDK.HttpError"
    error = str(caught.value).lower()
    assert "hostname" in error and ("doesn't match" in error or "mismatch" in error)
    assert names == [WRONG_HOSTNAME]
    assert requests == []


def test_rpc_rejects_untrusted_certificate(tls_endpoint, tls_material, monkeypatch):
    request, names, requests = tls_endpoint
    monkeypatch.setenv("ALIBABA_CLOUD_CA_BUNDLE", str(tls_material["unrelated-ca.pem"]))
    with pytest.raises(ClientException) as caught:
        request()
    assert caught.value.get_error_code() == "SDK.HttpError"
    error = str(caught.value).lower()
    assert "certificate_verify_failed" in error
    assert "issuer certificate" in error or "certificate chain" in error
    assert names == [HOSTNAME]
    assert requests == []
