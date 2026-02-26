import sys
import logging
import argparse
from app import create_app

log = logging.getLogger('werkzeug')
log.disabled = True
cli = sys.modules['flask.cli']
cli.show_server_banner = lambda *x: None

app = create_app()

def main() -> None:
    p = argparse.ArgumentParser(prog="linkloom")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8072)
    args = p.parse_args()

    app = create_app()
    print(f"LinkLoom starting on http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
