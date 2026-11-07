"""Management-authorized, single-use credential delivery; no worker sees a token.

All decisions and the only generation step run under the existing writer gate.
Exchanges store authorization metadata only. Lost responses require a NEW rotate
feedback, not a replay of this claim. Invalidation happens at stage commit, so
there is deliberately no usable token between rotation and successful claiming.
"""

from __future__ import annotations

import secrets

from ear_contracts import is_id, new_id
from ear_storage_client import InvalidRequest, PermissionDenied

from ear_storage.auth import IssuedPrincipal, _replace_grants, token_digest

CLAIM_TTL_SECONDS = 300


def stage_credentials(conn, feedback_id: str, module: str, *, revoke: bool) -> None:
    """Internal stage ONLY after management executor rechecks trusted authority.

    One stable principal per managed plugin (active or revoked). Legacy ambiguity
    fails closed rather than choosing an identity and stranding consumer ownership.
    First issuance copies explicitly approved module grants once; rotation never
    recreates grants. Core modules have no lifecycle row and cannot use this stage.
    """
    state = conn.execute(
        "SELECT revision,resources_ready FROM storage.module_lifecycle WHERE module=%s",
        (module,),
    ).fetchone()
    if state is None or (not revoke and not state[1]):
        raise InvalidRequest("credential_identity_not_ready")
    identities = conn.execute(
        "SELECT id FROM storage.principals WHERE module=%s ORDER BY id", (module,)
    ).fetchall()
    if revoke:
        # Revocation also contains ambiguous legacy identities without choosing or
        # deleting an owner. A later rotation still requires unambiguous identity.
        conn.execute(
            "UPDATE storage.principals SET active=false,token_hash=NULL,"
            "credential_epoch=credential_epoch+1 WHERE module=%s",
            (module,),
        )
        return
    if len(identities) > 1:
        raise InvalidRequest("credential_identity_not_ready")
    if not identities:
        pid = new_id()
        conn.execute(
            "INSERT INTO storage.principals(id,module,token_hash,active) VALUES (%s,%s,NULL,false)",
            (pid, module),
        )
        grants = conn.execute(
            "SELECT data_type,can_subscribe FROM storage.module_permissions WHERE module=%s",
            (module,),
        ).fetchall()
        _replace_grants(conn, pid, [g[0] for g in grants], [g[0] for g in grants if g[1]])
    else:
        pid = identities[0][0]
    row = conn.execute(
        "UPDATE storage.principals SET active=false,token_hash=NULL,"
        "credential_epoch=credential_epoch+1 WHERE id=%s RETURNING credential_epoch",
        (pid,),
    ).fetchone()
    assert row is not None
    if not revoke:
        conn.execute(
            "INSERT INTO storage.credential_exchanges"
            "(feedback_id,principal_id,credential_epoch,module_revision,expires_at) "
            "VALUES (%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second')",
            (feedback_id, pid, row[0], state[0], CLAIM_TTL_SECONDS),
        )


def claim_credentials(client, feedback_id: str) -> IssuedPrincipal:
    """Authenticated original active API admin only; commit precedes plaintext return."""
    from ear_storage.management import require_admin

    if not is_id(feedback_id):
        raise InvalidRequest("invalid feedback id")
    with client._session() as (conn, principal, _reader):
        require_admin(principal)
        row = conn.execute(
            "SELECT e.principal_id FROM storage.credential_exchanges e "
            "JOIN storage.management_tasks t USING(feedback_id) "
            "JOIN storage.management_authority a USING(feedback_id) "
            "JOIN storage.principals p ON p.id=e.principal_id "
            "JOIN storage.module_lifecycle m ON m.module=p.module "
            "WHERE e.feedback_id=%s AND a.principal_id=%s AND t.status='succeeded' "
            "AND e.status='pending' AND e.claimed_at IS NULL "
            "AND e.expires_at>clock_timestamp() AND m.resources_ready "
            "AND m.revision=e.module_revision AND p.credential_epoch=e.credential_epoch "
            "AND NOT p.active AND p.token_hash IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM storage.principals other "
            "WHERE other.module=p.module AND other.id<>p.id)",
            (feedback_id, principal.id),
        ).fetchone()
        if row is None:
            raise PermissionDenied("credential claim unavailable")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE storage.principals SET token_hash=%s,active=true WHERE id=%s",
            (token_digest(token), row[0]),
        )
        conn.execute(
            "UPDATE storage.credential_exchanges SET status='claimed',"
            "claimed_at=clock_timestamp() WHERE feedback_id=%s",
            (feedback_id,),
        )
    return IssuedPrincipal(row[0], token)
