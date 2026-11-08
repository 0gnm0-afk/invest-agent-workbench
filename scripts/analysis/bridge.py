"""Compatibility entry point for direct local-DB analysis calls. No HTTP/server."""
import json
import os

from workbench_paths import ROOT


def call(op, arguments):
    from analysis.direct import call as direct_call
    return direct_call(op, arguments, root=ROOT,
                       role=os.environ.get('INVEST_ANALYSIS_ROLE', 'collaboration'))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--analysis-key', required=True)
    parser.add_argument('--no-browser', action='store_true', help='Compatibility flag; direct DB reads never open a browser.')
    args = parser.parse_args()
    detail = call('detail', {'analysis_key': args.analysis_key})
    print(json.dumps({'analysis_key': detail['case']['id'], 'stock_key': detail['case']['stock_key'],
                      'links': detail['links'], 'transport': 'local_db'}, ensure_ascii=False))
