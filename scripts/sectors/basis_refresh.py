"""Recheck the pinned official basis before live collection; never mutate old evidence."""
import atomic_io as artifact_io
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sectors.basis import FAQ_URL, CONTRACT, load_basis

MAX_BYTES = 256 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('basis_document_redirect_refused')


def fetch(url):
    if url != FAQ_URL:
        raise ValueError('basis_document_url_not_allowed')
    try:
        with urllib.request.build_opener(NoRedirect()).open(
                urllib.request.Request(url, headers={'Accept':'text/markdown', 'Accept-Encoding':'identity'}),
                timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
            if response.status != 200 or response.geturl() != url:
                raise ValueError('basis_document_identity_mismatch')
            if not raw or len(raw) > MAX_BYTES:
                raise ValueError('basis_document_size_invalid')
            raw.decode('utf-8')
            return raw
    except (urllib.error.URLError, OSError, UnicodeError):
        raise ValueError('basis_document_refresh_unavailable') from None


def prepare(evidence, output, applied, *, fetcher=fetch):
    import sectors.observation as so
    config_path = evidence / 'calculation-basis.json'
    if not config_path.is_file():
        return None
    # Check the original identities/hashes/approval, without relaxing future-date checks.
    original = load_basis(evidence, artifact_io.checked, artifact_io.load, {}, applied,
                          lambda observed, when: 'future_observation'
                          if so.stamp(observed) > so.stamp(when) else None)
    stale = [d for d in original['evidence'] if so.age_issue(d['observed_at'], applied)]
    if not stale:
        return None
    output.mkdir(parents=True, exist_ok=True)
    documents = []
    for index, doc in enumerate(stale):
        if doc['url'] != FAQ_URL:
            raise ValueError('basis_document_url_not_allowed')
        raw = fetcher(doc['url'])
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
            raise ValueError('basis_document_size_invalid')
        name = f'document-{index}.md'
        with (output / name).open('xb') as stream:
            stream.write(raw)
        documents.append(dict(url=doc['url'], final_url=doc['url'], status=200,
            observed_at=datetime.now(timezone.utc).isoformat(), file=name,
            sha256=hashlib.sha256(raw).hexdigest()))
    receipt = dict(schema=1, contract=CONTRACT,
                   basis_sha256=artifact_io.sha(config_path), documents=documents)
    path = output / 'rechecks.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    # Changed content remains preserved for review, but cannot silently replace the contract.
    load_basis(evidence, artifact_io.checked, artifact_io.load, {}, datetime.now(timezone.utc).isoformat(),
               so.age_issue, rechecks=path)
    return path
