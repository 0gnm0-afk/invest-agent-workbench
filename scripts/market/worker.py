"""Own the collection lock in the process doing the work, independently of its caller."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from pathlib import Path
import runpy
import sys
from invest_agent.trading.store import instance_lock
from launch.journal import RunJournal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args, remaining = parser.parse_known_args()
    root = Path(args.root).resolve()
    if remaining[:1] == ['--']:
        remaining = remaining[1:]
    with RunJournal(root/'runtime/dashboard-daily/workers', 'market'):
        with instance_lock(root/'runtime/dashboard-daily/worker-lock'):
            script = root/'scripts/market/daily.py'
            sys.argv = [str(script), *remaining]
            runpy.run_path(str(script), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
