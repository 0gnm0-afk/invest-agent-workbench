"""Account coordination CLI for Codex while the official broker MCPs stay connected.

This never calls a broker. begin reserves a refresh; complete accepts only its
actual result. Read-only broker execution happens in the official MCP between them.
"""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import argparse
import json
import sys
import uuid
from accounts.store import AccountStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('begin','complete','saved'))
    parser.add_argument('--provider',required=True,choices=('nh','meritz'))
    parser.add_argument('--request-id')
    args=parser.parse_args()
    if args.operation=='complete' and not args.request_id:
        parser.error('complete requires --request-id')
    store=AccountStore()
    if args.operation=='begin':
        result=store.begin(args.provider,args.request_id or 'codex-'+uuid.uuid4().hex,'mcp')
    elif args.operation=='complete':
        # The lease and normalized actual result arrive on stdin, not a shell command line.
        value=json.load(sys.stdin)
        result=store.complete(args.provider,args.request_id,value['lease'],metadata=value['metadata'],snapshot=value.get('snapshot'))
    else:
        receipt=store.latest(args.provider)
        result={'receipt':receipt,'latest_attempt':store.latest_attempt(args.provider),
                'snapshot':store.read(receipt['snapshot_ref']) if receipt else None}
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))


if __name__=='__main__': main()
