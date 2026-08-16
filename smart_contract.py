"""
smart_contract.py — Cold-Chain Smart Contract Engine
======================================================
A self-contained backend module you can import into app.py (or any other
frontend). It is NOT deployed to a real blockchain — but the "digital
signature" part is real cryptography (RSA-2048 + SHA-256), not cosmetic.
Both parties sign the exact same terms; the contract only activates once
both signatures verify against the signed terms.

Lifecycle
---------
    DRAFT      -> terms proposed (unit id, temp range, deal amount, parties)
    SIGNED     -> both shipper and carrier have cryptographically signed
    ACTIVE     -> contract is now live, accepting temperature readings
    FINALIZED  -> shipment closed; an Invoice object has been generated

Typical usage
-------------
    from smart_contract import Party, SmartContract

    shipper = Party.create("Acme Cold Logistics", "shipper")
    carrier = Party.create("FreezeFleet Transport", "carrier")

    contract = SmartContract(
        unit_id="DEMO-01", shipper=shipper, carrier=carrier,
        deal_amount=10000, temp_min=24, temp_max=30,
    )

    contract.sign(shipper)
    contract.sign(carrier)          # status flips to SIGNED once both sign
    contract.activate()             # verifies both signatures, flips to ACTIVE

    contract.record_reading(31.2)   # feed live temperature readings in a loop
    contract.record_reading(31.5)
    contract.record_reading(29.0)   # back in range -> violation record closes

    invoice = contract.finalize()   # flips to FINALIZED, returns an Invoice
    print(invoice.to_text())

See the __main__ block at the bottom for a runnable end-to-end demo
(python smart_contract.py).
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.exceptions import InvalidSignature


# ---------------------------------------------------------------------------
# Penalty schedule — single source of truth, mirrors the original dashboard
# ---------------------------------------------------------------------------
PENALTY_TIERS = [  # (duration_sec_threshold, penalty_percent), checked highest first
    (300, 50),
    (120, 25),
    (60, 10),
    (0, 0),
]


def calculate_penalty_percent(duration_sec: float) -> int:
    for threshold, pct in PENALTY_TIERS:
        if duration_sec >= threshold:
            return pct
    return 0


def calculate_deduction(amount: float, percent: int) -> int:
    return round((amount or 0) * percent / 100)


def calculate_final_payment(amount: float, deduction: int) -> int:
    return max(0, round(amount or 0) - deduction)


def format_duration_words(sec: float) -> str:
    s = max(0, round(sec))
    m, r = divmod(s, 60)
    if m == 0:
        return f"{r} second{'' if r == 1 else 's'}"
    if r == 0:
        return f"{m} minute{'' if m == 1 else 's'}"
    return f"{m} minute{'' if m == 1 else 's'} {r} second{'' if r == 1 else 's'}"


class ContractStatus(Enum):
    DRAFT = "draft"
    SIGNED = "signed"
    ACTIVE = "active"
    FINALIZED = "finalized"


# ---------------------------------------------------------------------------
# Party — one signer, holding a real RSA keypair (stands in for a wallet)
# ---------------------------------------------------------------------------
@dataclass
class Party:
    name: str
    role: str  # "shipper" or "carrier" (any two distinct labels work)
    _private_key: rsa.RSAPrivateKey = field(repr=False)
    public_key: rsa.RSAPublicKey = field(init=False)

    def __post_init__(self):
        self.public_key = self._private_key.public_key()

    @classmethod
    def create(cls, name: str, role: str) -> "Party":
        """Generates a fresh RSA-2048 keypair for this party (like provisioning a wallet)."""
        pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return cls(name=name, role=role, _private_key=pk)

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )

    def fingerprint(self) -> str:
        """Short public identifier for this party's key, safe to display in UI/ledger."""
        pub_bytes = self.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return "0x" + hashlib.sha256(pub_bytes).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ViolationRecord — one open/closed breach window
# ---------------------------------------------------------------------------
@dataclass
class ViolationRecord:
    seq: int
    start_time: datetime
    temp_at_start: float
    end_time: Optional[datetime] = None
    min_temp: float = field(default=0.0)
    max_temp: float = field(default=0.0)
    duration_sec: float = 0.0
    status: str = "active"
    tx_hash: Optional[str] = None
    block: Optional[int] = None

    def __post_init__(self):
        self.min_temp = self.temp_at_start
        self.max_temp = self.temp_at_start

    def update(self, temp: float, now: datetime):
        self.duration_sec = (now - self.start_time).total_seconds()
        self.min_temp = min(self.min_temp, temp)
        self.max_temp = max(self.max_temp, temp)

    def close(self, now: datetime):
        self.duration_sec = (now - self.start_time).total_seconds()
        self.end_time = now
        self.status = "closed"


# ---------------------------------------------------------------------------
# Invoice — produced by SmartContract.finalize()
# ---------------------------------------------------------------------------
@dataclass
class Invoice:
    contract_id: str
    unit_id: str
    generated_at: datetime
    deal_amount: float
    temp_min: float
    temp_max: float
    worst_violation_duration: float
    penalty_percent: int
    deducted_amount: int
    final_payment: int
    violations: list
    violated: bool
    reason: str
    contract_hash: str
    shipper_fingerprint: str
    carrier_fingerprint: str

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "unit_id": self.unit_id,
            "generated_at": self.generated_at.isoformat(),
            "deal_amount": self.deal_amount,
            "temp_range": f"{self.temp_min}-{self.temp_max}",
            "worst_violation_duration_sec": self.worst_violation_duration,
            "penalty_percent": self.penalty_percent,
            "deducted_amount": self.deducted_amount,
            "final_payment": self.final_payment,
            "violation_count": len(self.violations),
            "violated": self.violated,
            "reason": self.reason,
            "contract_hash": self.contract_hash,
            "shipper_fingerprint": self.shipper_fingerprint,
            "carrier_fingerprint": self.carrier_fingerprint,
        }

    def to_text(self) -> str:
        lines = [
            "=" * 52,
            "COLD-CHAIN SHIPMENT INVOICE",
            "=" * 52,
            f"Contract ID        : {self.contract_id}",
            f"Shipment unit       : {self.unit_id}",
            f"Generated at        : {self.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Original deal amount: Rs.{self.deal_amount:,.0f}",
            f"Temperature rule    : {self.temp_min}-{self.temp_max} C",
            f"Violation duration  : {format_duration_words(self.worst_violation_duration)}",
            f"Violation status    : {'VIOLATED' if self.violated else 'No violation'}",
            f"Penalty applied     : {self.penalty_percent}%",
            f"Deducted amount     : Rs.{self.deducted_amount:,.0f}",
            f"FINAL PAYMENT       : Rs.{self.final_payment:,.0f}",
            "-" * 52,
            f"Reason: {self.reason}",
            "-" * 52,
            f"Contract hash: {self.contract_hash}",
            f"Shipper key  : {self.shipper_fingerprint}",
            f"Carrier key  : {self.carrier_fingerprint}",
            "=" * 52,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SmartContract — the core state machine
# ---------------------------------------------------------------------------
class SmartContract:
    def __init__(self, unit_id: str, shipper: Party, carrier: Party,
                 deal_amount: float, temp_min: float, temp_max: float):
        self.unit_id = unit_id
        self.shipper = shipper
        self.carrier = carrier
        self.deal_amount = deal_amount
        self.temp_min = temp_min
        self.temp_max = temp_max
        self.created_at = datetime.now()
        self.status = ContractStatus.DRAFT

        self._signatures: dict[str, bytes] = {}
        self.violations: list[ViolationRecord] = []
        self._active_violation: Optional[ViolationRecord] = None
        self.invoice: Optional[Invoice] = None

        # contract_id is derived from the terms themselves -> deterministic + tamper evident
        self.contract_id = "CC-" + hashlib.sha256(self._terms_payload()).hexdigest()[:10].upper()
        self._block_counter = 481920

    # -- terms & signing ----------------------------------------------------
    def _terms_payload(self) -> bytes:
        """Canonical byte representation of the contract terms. This exact
        payload is what each party signs — change any term and every
        existing signature becomes invalid."""
        payload = {
            "unit_id": self.unit_id,
            "shipper": self.shipper.name,
            "carrier": self.carrier.name,
            "deal_amount": self.deal_amount,
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "created_at": self.created_at.isoformat(),
        }
        return json.dumps(payload, sort_keys=True).encode()

    def sign(self, party: Party):
        """Called once per party. Raises if the contract has moved past DRAFT/SIGNED,
        or if this party's role has already signed."""
        if self.status not in (ContractStatus.DRAFT, ContractStatus.SIGNED):
            raise ValueError(f"Cannot sign a contract in status {self.status.value}")
        if party.role not in (self.shipper.role, self.carrier.role):
            raise ValueError(f"{party.name} is not a party to this contract")
        self._signatures[party.role] = party.sign(self._terms_payload())
        if len(self._signatures) == 2:
            self.status = ContractStatus.SIGNED

    def is_fully_signed(self) -> bool:
        return len(self._signatures) == 2

    def has_signed(self, role: str) -> bool:
        return role in self._signatures

    def verify_signatures(self) -> bool:
        """Cryptographically re-checks both signatures against the terms payload.
        Returns False if either is missing or invalid (e.g. terms were tampered with)."""
        payload = self._terms_payload()
        for party in (self.shipper, self.carrier):
            sig = self._signatures.get(party.role)
            if sig is None:
                return False
            try:
                party.public_key.verify(
                    sig, payload,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                    hashes.SHA256(),
                )
            except InvalidSignature:
                return False
        return True

    def activate(self):
        if self.status != ContractStatus.SIGNED:
            raise ValueError("Both parties must sign before the contract can activate")
        if not self.verify_signatures():
            raise ValueError("Signature verification failed — contract terms may have been tampered with")
        self.status = ContractStatus.ACTIVE

    # -- condition monitoring -------------------------------------------------
    def record_reading(self, temp: float, timestamp: Optional[datetime] = None):
        """Feed one live temperature reading in. Call this on every sensor tick
        while status == ACTIVE. Opens a ViolationRecord on breach, updates it
        live while still breaching, and closes it on recovery — mirrors the
        original dashboard's ledger behaviour exactly."""
        if self.status != ContractStatus.ACTIVE:
            return
        now = timestamp or datetime.now()
        out_of_range = temp < self.temp_min or temp > self.temp_max

        if out_of_range:
            if self._active_violation is None:
                v = ViolationRecord(seq=len(self.violations) + 1, start_time=now, temp_at_start=temp)
                seed = f"{self.contract_id}|{self.unit_id}|{v.seq}|{now.isoformat()}|{temp:.2f}"
                v.tx_hash = "0x" + hashlib.sha256(seed.encode()).hexdigest()[:16]
                v.block = self._block_counter
                self._block_counter += 1
                self.violations.append(v)
                self._active_violation = v
            else:
                self._active_violation.update(temp, now)
        else:
            if self._active_violation is not None:
                self._active_violation.close(now)
                self._active_violation = None

    def worst_violation_duration(self) -> float:
        durations = [v.duration_sec for v in self.violations]
        return max(durations) if durations else 0.0

    @property
    def active_violation(self) -> Optional[ViolationRecord]:
        return self._active_violation

    def current_violation_duration(self) -> float:
        """Duration of the violation happening right now, 0 if none is active."""
        return self._active_violation.duration_sec if self._active_violation else 0.0

    def current_penalty_percent(self) -> int:
        """Live projected penalty based on the worst violation seen so far
        (matches the dashboard's 'projected settlement' figures before finalize)."""
        duration = self._active_violation.duration_sec if self._active_violation else self.worst_violation_duration()
        return calculate_penalty_percent(duration)

    # -- finalization & invoicing --------------------------------------------
    def finalize(self) -> Invoice:
        if self.status != ContractStatus.ACTIVE:
            raise ValueError(f"Cannot finalize a contract in status {self.status.value}")

        now = datetime.now()
        if self._active_violation is not None:
            self._active_violation.close(now)
            self._active_violation = None

        worst = self.worst_violation_duration()
        percent = calculate_penalty_percent(worst)
        deducted = calculate_deduction(self.deal_amount, percent)
        final_payment = calculate_final_payment(self.deal_amount, deducted)
        violated = worst > 0

        reason = (
            f"Temperature remained outside the permitted range for {format_duration_words(worst)}."
            if violated else
            "Temperature remained within the permitted range for the entire shipment."
        )

        contract_hash = hashlib.sha256(
            self._terms_payload() + f"|FINAL|{worst}|{now.isoformat()}".encode()
        ).hexdigest()

        self.invoice = Invoice(
            contract_id=self.contract_id,
            unit_id=self.unit_id,
            generated_at=now,
            deal_amount=self.deal_amount,
            temp_min=self.temp_min,
            temp_max=self.temp_max,
            worst_violation_duration=worst,
            penalty_percent=percent,
            deducted_amount=deducted,
            final_payment=final_payment,
            violations=self.violations,
            violated=violated,
            reason=reason,
            contract_hash="0x" + contract_hash,
            shipper_fingerprint=self.shipper.fingerprint(),
            carrier_fingerprint=self.carrier.fingerprint(),
        )
        self.status = ContractStatus.FINALIZED
        return self.invoice


# ---------------------------------------------------------------------------
# Runnable demo — python smart_contract.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    shipper = Party.create("Acme Cold Logistics", "shipper")
    carrier = Party.create("FreezeFleet Transport", "carrier")

    contract = SmartContract(
        unit_id="DEMO-01", shipper=shipper, carrier=carrier,
        deal_amount=10000, temp_min=24, temp_max=30,
    )
    print(f"Contract drafted: {contract.contract_id}  status={contract.status.value}")

    contract.sign(shipper)
    print(f"Signed by shipper. status={contract.status.value}")
    contract.sign(carrier)
    print(f"Signed by carrier. status={contract.status.value}")

    contract.activate()
    print(f"Activated. Signatures valid: {contract.verify_signatures()}  status={contract.status.value}")

    # simulate a temperature feed over time: in range, then a ~95s breach, then recovery
    from datetime import timedelta
    base = datetime.now()
    readings = [
        (0,  27),   # in range
        (5,  28),   # in range
        (10, 31.5), # breach starts
        (40, 32),   # still breaching
        (75, 31.2), # still breaching (now ~65s in -> 10% tier)
        (95, 31.0), # still breaching (now ~85s in)
        (100, 29),  # recovers -> violation closes at ~90s -> 10% tier
    ]
    for offset_sec, temp in readings:
        contract.record_reading(temp, timestamp=base + timedelta(seconds=offset_sec))

    invoice = contract.finalize()
    print()
    print(invoice.to_text())
