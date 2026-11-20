"""FlowPilot AI Cryptographic Approval Token Security Module.

Guarantees:
- Cryptographically secure tokens generated via `secrets.token_urlsafe`
- Storage of SHA-256 token hashes in SQLite (raw token is never persisted)
- Binding to Run ID, Task ID, and deterministic SHA-256 action payload hash
- Strict TTL expiration
- Atomic single-use consumption preventing token reuse and concurrent double-approvals
- Tampering detection for altered action payloads
"""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
from typing import Any, Dict, Optional
import uuid

from ..config import settings
from ..observability.logger import get_logger
from ..storage.repository import WorkflowRepository, repository as default_repository
from .exceptions import (
    ApprovalConsumedException,
    ApprovalException,
    ApprovalExpiredException,
    ApprovalNotFoundException,
    ApprovalTamperingException,
)

logger = get_logger("flowpilot.safety.approval")


def hash_token(raw_token: str) -> str:
    """Compute SHA-256 hash of raw token string."""
    if not raw_token or not isinstance(raw_token, str):
        raise ApprovalException("Token must be a non-empty string.")
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def hash_payload(payload: Any) -> str:
    """Compute deterministic SHA-256 hash of an action payload."""
    if payload is None:
        canonical = "{}"
    elif isinstance(payload, str):
        canonical = payload.strip()
    elif isinstance(payload, (dict, list)):
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    else:
        canonical = str(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalTokenManager:
    """Manages issuing, verification, and single-use atomic consumption of approval tokens."""

    def __init__(
        self,
        repository: Optional[WorkflowRepository] = None,
        ttl_seconds: Optional[int] = None,
    ):
        self.repository = repository or default_repository
        self.ttl_seconds = (
            settings.approval_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        if self.ttl_seconds <= 0:
            raise ValueError("Approval token lifetime must be positive.")

    async def issue_token(
        self,
        run_id: str,
        task_id: str,
        action_payload: Any,
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Issue a cryptographically secure approval token.

        Stores the token and payload hashes, not the raw token or action payload.
        The raw token is returned to the caller once.
        """
        raw_token = f"fp_tok_{secrets.token_urlsafe(32)}"
        token_hash = hash_token(raw_token)
        payload_hash = hash_payload(action_payload)

        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=effective_ttl)
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"

        # Persist hash to database
        await self.repository.create_approval(
            approval_id=approval_id,
            token=token_hash,  # Only token_hash is stored!
            run_id=run_id,
            task_id=task_id,
            payload_hash=payload_hash,
            action_payload={},
            expires_at=expires_at,
        )

        logger.info(
            f"Issued approval token for run '{run_id}', task '{task_id}'. TTL: {effective_ttl}s.",
            extra={
                "run_id": run_id,
                "task_id": task_id,
                "data": {"approval_id": approval_id, "expires_at": expires_at.isoformat()},
            },
        )

        return {
            "approval_id": approval_id,
            "raw_token": raw_token,
            "token_hash": token_hash,
            "run_id": run_id,
            "task_id": task_id,
            "payload_hash": payload_hash,
            "expires_at": expires_at.isoformat(),
        }

    async def verify_and_consume(
        self,
        raw_token: str,
        action: str = "APPROVE",
        decision_by: str = "operator",
        *,
        expected_payload: Any,
    ) -> Dict[str, Any]:
        """Verify token validity, payload binding, and atomically consume it.

        Raises:
            ApprovalNotFoundException: If token hash does not exist.
            ApprovalConsumedException: If token was already approved or rejected.
            ApprovalExpiredException: If token has passed expiration.
            ApprovalTamperingException: If provided action payload does not match token binding.
        """
        action_norm = action.upper().strip()
        if action_norm not in ("APPROVE", "REJECT"):
            raise ApprovalException(f"Invalid action '{action}'. Must be 'APPROVE' or 'REJECT'.")
        if expected_payload is None:
            raise ApprovalTamperingException(
                "An expected action payload is required to verify this approval."
            )

        token_hash = hash_token(raw_token)

        # 1. Fetch approval by token hash
        record = await self.repository.get_approval(token_hash)
        if record is None:
            logger.warning("Approval attempt failed: token not found.")
            raise ApprovalNotFoundException("Invalid or non-existent approval token.")

        # 2. Check if already consumed
        if record.get("status") != "PENDING" or record.get("consumed_at") is not None:
            logger.warning(
                f"Approval attempt failed: token already consumed with status '{record.get('status')}'.",
                extra={"run_id": record.get("run_id"), "task_id": record.get("task_id")},
            )
            raise ApprovalConsumedException(
                f"Approval token has already been consumed (status: {record.get('status')})."
            )

        # 3. Check expiration
        expires_at_val = record.get("expires_at")
        if isinstance(expires_at_val, str):
            # Parse SQLite timestamp
            try:
                expires_at_dt = datetime.fromisoformat(expires_at_val)
            except ValueError:
                # Handle space-separated SQL timestamp
                expires_at_dt = datetime.strptime(expires_at_val, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
        elif isinstance(expires_at_val, datetime):
            expires_at_dt = expires_at_val
        else:
            raise ApprovalException("Corrupted expiration timestamp on approval record.")

        if expires_at_dt.tzinfo is None:
            expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) > expires_at_dt:
            logger.warning(
                f"Approval attempt failed: token expired at {expires_at_dt.isoformat()}.",
                extra={"run_id": record.get("run_id"), "task_id": record.get("task_id")},
            )
            raise ApprovalExpiredException(
                f"Approval token has expired (expired at {expires_at_dt.isoformat()})."
            )

        # 4. Verify payload hash match
        candidate_hash = hash_payload(expected_payload)
        if not hmac.compare_digest(candidate_hash, str(record.get("payload_hash", ""))):
            logger.error(
                "Approval payload mismatch: attempted approval does not match bound action payload.",
                extra={"run_id": record.get("run_id"), "task_id": record.get("task_id")},
            )
            raise ApprovalTamperingException(
                "Payload mismatch: Attempted approval does not match the token's bound action payload."
            )

        # 5. Atomic database consumption checks pending status and expiry again.
        target_status = "APPROVED" if action_norm == "APPROVE" else "REJECTED"
        consumed = await self.repository.consume_approval(
            token=token_hash,
            status=target_status,
            decision_by=decision_by,
        )

        if consumed is None:
            # Atomic transition failed: either a concurrent race consumed it first or it expired
            recheck = await self.repository.get_approval(token_hash)
            if recheck and recheck.get("status") != "PENDING":
                raise ApprovalConsumedException(
                    f"Concurrent approval attempt: Token was already consumed ({recheck.get('status')})."
                )
            raise ApprovalExpiredException("Token expired before atomic consumption completed.")

        logger.info(
            f"Approval token successfully consumed. Action: {target_status} by {decision_by}.",
            extra={
                "run_id": consumed.get("run_id"),
                "task_id": consumed.get("task_id"),
                "data": {"status": target_status, "decision_by": decision_by},
            },
        )
        return consumed


# Global default manager
approval_manager = ApprovalTokenManager()
