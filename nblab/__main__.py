"""Command line:  python -m nblab <command>

  design            show what the intent produces (offline, no NetBox)
  plan              compare design with NetBox, change nothing
  apply [--prune]   make NetBox match the design (--prune also deletes orphans)
  check             validate the data in NetBox (topology, VLANs, IPs, naming)
  render            generate switch configs from NetBox into configs/<site>/
  reset --yes       delete everything tagged lab-managed
  setup-gui         install the Custom Scripts + Config Template into NetBox (GUI workflow)
"""

import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(prog="nblab", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("design")
    sp = sub.add_parser("plan")
    sp.add_argument("-v", "--verbose", action="store_true")
    sa = sub.add_parser("apply")
    sa.add_argument("--prune", action="store_true", help="delete tagged objects not in the design")
    sa.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("check")
    sr = sub.add_parser("render")
    sr.add_argument("--force", action="store_true", help="render even if 'check' finds errors")
    sr.add_argument("--device", help="render only this device")
    sx = sub.add_parser("reset")
    sx.add_argument("--yes", action="store_true")
    sub.add_parser("setup-gui")
    args = p.parse_args()

    if args.cmd == "design":
        from .design import summary
        from .intent import build_from_repo
        print(json.dumps(summary(build_from_repo()), indent=2))
        return 0

    from .config import connect
    nb = connect()

    if args.cmd in ("plan", "apply"):
        from .sync import Syncer
        ok = Syncer(nb, apply=args.cmd == "apply", prune=getattr(args, "prune", False),
                    verbose=args.verbose).run()
        return 0 if ok else 1
    if args.cmd == "check":
        from .checks import run_checks
        return 0 if run_checks(nb) else 1
    if args.cmd == "render":
        from .render import render_all
        return 0 if render_all(nb, force=args.force, only=args.device) else 1
    if args.cmd == "setup-gui":
        from .setup_gui import setup_gui
        return setup_gui(nb)
    if args.cmd == "reset":
        from .reset import reset
        return reset(nb, args.yes)
    return 1


if __name__ == "__main__":
    sys.exit(main())
