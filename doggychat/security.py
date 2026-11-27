import contextlib
import base64
import json
import os
import ssl
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _hello_payload(identity_id, member_id, request_id, port):
    return json.dumps(
        [identity_id, member_id, request_id, port],
        separators=(",", ":"),
    ).encode("utf-8")


class TLSIdentity:
    def __init__(self, identity_id, certificate_path=None, private_key_path=None):
        self.identity_id = identity_id
        self._temporary_directory = None
        if certificate_path is None or private_key_path is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="doggychat-tls-"
            )
            root = Path(self._temporary_directory.name)
            certificate_path = root / "identity.crt"
            private_key_path = root / "identity.key"
        self.certificate_path = Path(certificate_path)
        self.private_key_path = Path(private_key_path)
        self._load_or_create()

    @property
    def fingerprint(self):
        return self.certificate.fingerprint(hashes.SHA256()).hex()

    @property
    def certificate_pem(self):
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode(
            "ascii"
        )

    def server_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            str(self.certificate_path),
            str(self.private_key_path),
        )
        return context

    @staticmethod
    def client_context():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def sign_hello(self, member_id, request_id, port):
        signature = self.private_key.sign(
            _hello_payload(self.identity_id, member_id, request_id, port),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "tls_identity_id": self.identity_id,
            "tls_certificate": self.certificate_pem,
            "tls_signature": base64.b64encode(signature).decode("ascii"),
        }

    def cleanup(self):
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def _load_or_create(self):
        if self.certificate_path.exists() and self.private_key_path.exists():
            self.certificate = x509.load_pem_x509_certificate(
                self.certificate_path.read_bytes()
            )
            self.private_key = serialization.load_pem_private_key(
                self.private_key_path.read_bytes(),
                password=None,
            )
            certificate_key = self.certificate.public_key()
            if (
                not isinstance(certificate_key, ec.EllipticCurvePublicKey)
                or not isinstance(self.private_key, ec.EllipticCurvePrivateKey)
                or not isinstance(
                    self.private_key.curve,
                    ec.SECP256R1,
                )
                or not isinstance(certificate_key.curve, ec.SECP256R1)
            ):
                raise ValueError("Unsupported DoggyChat TLS identity key")
            common_names = self.certificate.subject.get_attributes_for_oid(
                NameOID.COMMON_NAME
            )
            if (
                len(common_names) != 1
                or common_names[0].value != self.identity_id
                or certificate_key.public_numbers()
                != self.private_key.public_key().public_numbers()
            ):
                raise ValueError("TLS key does not match the DoggyChat identity")
            return

        self.certificate_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = ec.generate_private_key(ec.SECP256R1())
        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, self.identity_id)]
        )
        now = datetime.utcnow()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256())
        )
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self._write_private_file(self.private_key_path, key_pem)
        self._write_private_file(
            self.certificate_path,
            certificate.public_bytes(serialization.Encoding.PEM),
        )
        self.certificate = certificate
        self.private_key = private_key

    @staticmethod
    def _write_private_file(path, contents):
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(contents)
            os.replace(temporary, path)
        except OSError:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


class TrustStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._fingerprints = self._read()

    def trust(self, identity_id, fingerprint):
        if (
            not _is_uuid(identity_id)
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise ValueError("Invalid DoggyChat TLS fingerprint")
        with self._lock:
            if self.path is None:
                return self._trust_locked(identity_id, fingerprint)
            with _identity_file_lock(
                self.path.with_name(self.path.name + ".lock")
            ):
                self._fingerprints = self._read()
                return self._trust_locked(identity_id, fingerprint)

    def _trust_locked(self, identity_id, fingerprint):
        existing = self._fingerprints.get(identity_id)
        if existing is not None:
            return "known" if existing == fingerprint else "changed"
        self._fingerprints[identity_id] = fingerprint
        try:
            self._save()
        except OSError:
            del self._fingerprints[identity_id]
            raise
        return "new"

    def _read(self):
        if self.path is None:
            return {}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (
            not isinstance(stored, dict)
            or any(
                not isinstance(identity_id, str)
                or not _is_uuid(identity_id)
                or not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
                for identity_id, fingerprint in stored.items()
            )
        ):
            raise ValueError(f"Invalid DoggyChat trust store: {self.path}")
        return stored

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self._fingerprints, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def load_or_create_local_identity(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / "identity.json"
    certificate_path = directory / "identity.crt"
    private_key_path = directory / "identity.key"
    with _identity_file_lock(directory / "identity.lock"):
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            identity_id = uuid.uuid4().hex
            data = {"identity_id": identity_id}
        if (
            not isinstance(data, dict)
            or not _is_uuid(data.get("identity_id"))
        ):
            raise ValueError(f"Invalid DoggyChat identity file: {identity_path}")
        identity_id = data["identity_id"]
        if not identity_path.exists():
            TLSIdentity._write_private_file(
                identity_path,
                json.dumps(data, separators=(",", ":")).encode("utf-8"),
            )
        identity = TLSIdentity(
            identity_id,
            certificate_path,
            private_key_path,
        )
        return identity


@contextlib.contextmanager
def _identity_file_lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(
                        lock_file.fileno(),
                        msvcrt.LK_LOCK,
                        1,
                    )
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(
                    lock_file.fileno(),
                    msvcrt.LK_UNLCK,
                    1,
                )
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def verify_hello_identity(frame):
    identity_id = frame.get("id")
    tls_identity_id = frame.get("tls_identity_id")
    request_id = frame.get("request_id")
    port = frame.get("port")
    certificate_pem = frame.get("tls_certificate")
    signature_text = frame.get("tls_signature")
    if (
        not _is_uuid(identity_id)
        or not _is_uuid(tls_identity_id)
        or not isinstance(request_id, str)
        or not _is_uuid(request_id)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(certificate_pem, str)
        or len(certificate_pem) > 8192
        or not isinstance(signature_text, str)
        or len(signature_text) > 1024
    ):
        raise ValueError("Missing or invalid TLS identity")
    try:
        certificate = x509.load_pem_x509_certificate(
            certificate_pem.encode("ascii")
        )
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("Invalid TLS identity certificate") from error
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if (
        certificate.issuer != certificate.subject
        or len(common_names) != 1
        or common_names[0].value != tls_identity_id
    ):
        raise ValueError("TLS certificate does not match the user identity")
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError("Unsupported TLS identity key")
    try:
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm),
        )
        public_key.verify(
            signature,
            _hello_payload(tls_identity_id, identity_id, request_id, port),
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError, UnsupportedAlgorithm) as error:
        raise ValueError("TLS identity signature is invalid") from error
    return certificate.fingerprint(hashes.SHA256()).hex()


def _is_uuid(value):
    try:
        return uuid.UUID(hex=value).hex == value
    except (ValueError, TypeError, AttributeError):
        return False
