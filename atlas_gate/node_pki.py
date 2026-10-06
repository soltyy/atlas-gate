"""Операторский выпуск серверного TLS-сертификата; не меняет службы или БД Gate."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def issue_server(ca_directory, directory, hosts):
    ca_dir, root = Path(ca_directory), Path(directory)
    ca = x509.load_pem_x509_certificate((ca_dir / "ca.pem").read_bytes())
    authority = serialization.load_pem_private_key((ca_dir / "ca-key.pem").read_bytes(), password=None)
    key = ed25519.Ed25519PrivateKey.generate()
    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if "://" in host or "/" in host or "*" in host:
                raise ValueError("hosts должны быть точными DNS/IP")
            names.append(x509.DNSName(host))
    if not names:
        raise ValueError("нужен хотя бы один DNS/IP")
    now = datetime.now(timezone.utc)
    certificate = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Atlas TLS server")])).issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(x509.SubjectAlternativeName(names), critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True).sign(authority, algorithm=None)
    root.mkdir(parents=True, exist_ok=True)
    # Не затирать рабочие файлы: оператор переключает новую пару после проверки.
    with (root / "server-key.pem").open("xb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (root / "server-key.pem").chmod(0o600)
    with (root / "server-cert.pem").open("xb") as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))
    return root / "server-cert.pem", root / "server-key.pem"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ca-directory", required=True)
    p.add_argument("--action", choices=("init", "server"), default="server")
    p.add_argument("--directory")
    p.add_argument("--hosts", nargs="+")
    args = p.parse_args()
    if args.action == "init":
        from .gate.node_identity import NodeIdentity
        from .gate.store import Store
        from types import SimpleNamespace
        store = Store(":memory:")
        try:
            _, cert = NodeIdentity(store, SimpleNamespace(config={}, snapshots={}), args.ca_directory, "pki-offline").ca()
            from cryptography.hazmat.primitives import hashes
            print("CA SHA-256:", cert.fingerprint(hashes.SHA256()).hex())
        finally:
            store.close()
        return
    if not args.directory or not args.hosts:
        p.error("server требует --directory и --hosts")
    cert, key = issue_server(args.ca_directory, args.directory, args.hosts)
    print("Созданы сертификат и закрытый ключ в", cert.parent)


if __name__ == "__main__":
    main()
