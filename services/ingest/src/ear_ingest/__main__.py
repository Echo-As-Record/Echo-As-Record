import argparse
import os
import subprocess
from pathlib import Path

import segno
import uvicorn

from ear_ingest import native
from ear_ingest.app import create_app
from ear_ingest.pairing import DEFAULT_HOME, Pairing


def main() -> None:
    parser = argparse.ArgumentParser(prog="ear-ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8791)
    serve.add_argument(
        "--paired", action="store_true", help="Enable TLS and advertise with Bonjour"
    )
    relay = sub.add_parser("relay", help="Loopback HTTP listener for encrypted FRP uploads")
    relay.add_argument("--port", type=int, default=18873)
    sub.add_parser("octopus-finalized", help="Accept one trusted finalized Octopus event on stdin")
    pair = sub.add_parser("pair")
    pair.add_argument(
        "--relay",
        default=os.getenv("EAR_INGEST_RELAY_URL", ""),
        help="Configured HTTPS or encrypted HTTP relay",
    )
    pair.add_argument("--output", type=Path, help="Write the pairing QR to a private PNG")
    args = parser.parse_args()
    if args.command == "octopus-finalized":
        from ear_ingest.octopus_finalized import main as accept_finalized

        raise SystemExit(accept_finalized())
    if args.command == "serve" and not args.paired:
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return
    if args.command != "pair":
        native.config()  # Reject mixed configuration before opening pairing state.
    pairing = Pairing(Path(os.getenv("EAR_PAIRING_HOME", DEFAULT_HOME)).expanduser())
    if args.command == "relay":
        from ear_ingest.relay import create_relay

        uvicorn.run(create_relay(pairing, create_app(pairing)), host="127.0.0.1", port=args.port)
        return
    if args.command == "pair":
        qr = segno.make(pairing.invite(args.relay), micro=False)
        if args.output:
            with args.output.open("xb") as f:
                os.fchmod(f.fileno(), 0o600)
                qr.save(f, kind="png", scale=6)
        else:
            qr.terminal(compact=True)
        return
    app = create_app(pairing)
    cert, key, _ = pairing.certificate()
    with subprocess.Popen(
        ["/usr/bin/dns-sd", "-R", pairing.id, "_ear-ingest._tcp", "local.", str(args.port)],
        stdout=subprocess.DEVNULL,
    ) as advertisement:
        try:
            uvicorn.run(
                app, host=args.host, port=args.port, ssl_certfile=str(cert), ssl_keyfile=str(key)
            )
        finally:
            advertisement.terminate()


if __name__ == "__main__":
    main()
