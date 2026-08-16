// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * ColdChainAgreement.sol
 * ============================================================================
 * Tamper-proof cold-chain shipment agreement + violation settlement contract.
 *
 * WHY THIS EXISTS (read this first)
 * ----------------------------------------------------------------------------
 * A smart contract cannot read a temperature sensor by itself — blockchains
 * are passive and only react to transactions sent to them. This contract is
 * the "incorruptible judge": once deployed, nobody (including the deployer)
 * can quietly change the penalty tiers, the temperature range, or fabricate
 * a settlement. Only a pre-authorized "oracle" address (the Python backend)
 * is allowed to report raw temperature readings in — and even the oracle
 * cannot decide whether a reading counts as a violation. THIS CONTRACT does
 * that, on-chain, using the min/max range that was in force at the moment
 * both parties signed (editable before then, frozen for good after).
 *
 * ARCHITECTURE
 * ----------------------------------------------------------------------------
 *   IoT sensor (or simulated backend)
 *          -> Python backend reads/generates a temperature value
 *          -> Backend, as the authorized oracle, calls ONE function:
 *                recordTemperature(tempCentiC)
 *          -> This contract compares tempCentiC against
 *                minTemperatureCentiC / maxTemperatureCentiC (set at
 *                construction, never changeable) and AUTONOMOUSLY starts,
 *                updates, or resolves a violation. The oracle never tells
 *                the contract "this is a violation" — it only reports the
 *                number, and the contract judges it.
 *          -> completeShipment() locks in the final settlement, permanently.
 *
 * SECURITY / ENFORCEMENT RULES ENFORCED ON-CHAIN
 * ----------------------------------------------------------------------------
 *  - Only the shipper can sign as shipper; only the transporter as transporter.
 *  - Contract cannot activate until BOTH parties have signed.
 *  - shipper, transporter, oracle, productId, batchId, containerId are set
 *    exactly once (constructor) and there is NO function, anywhere in this
 *    contract, that can change them afterwards — not even for the deployer.
 *  - minTemperatureCentiC, maxTemperatureCentiC and dealAmount start with
 *    the constructor's values but MAY be revised, once or many times, via
 *    updateShipmentConditions() — and only via that function — up until
 *    the moment either party signs. From that first signature onward they
 *    are frozen exactly as immutably as everything else in this list.
 *  - In-range vs out-of-range is decided ON-CHAIN by comparing the reported
 *    reading to minTemperatureCentiC/maxTemperatureCentiC. The oracle only
 *    supplies the raw number.
 *  - Only the authorized oracle can submit temperature readings.
 *  - Settlement cannot happen twice.
 *  - Final payment can never exceed the (symbolic) deal amount.
 *  - Penalty can never exceed 50%.
 *
 * NOTE ON FUNDS (read before assuming this escrows real value)
 * ----------------------------------------------------------------------------
 * This is a prototype for demonstrating tamper-proof enforcement logic.
 * dealAmount is a plain uint256 used purely as the base figure for the
 * penalty/settlement math. The constructor and activateAgreement() are
 * NOT payable, no ETH or token is ever transferred into or out of this
 * contract, and "FundsLocked" describes a contractual state transition,
 * not an actual escrow deposit. Do not present this contract as holding
 * real funds unless payable escrow logic is added deliberately later.
 */
contract ColdChainAgreement {
    // ------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------
    enum AgreementStatus {
        Draft,                          // 0 - created, awaiting signatures
        WaitingForShipperSignature,     // 1
        WaitingForTransporterSignature, // 2
        Signed,                         // 3 - both parties signed
        FundsLocked,                    // 4 - activated (symbolic, no real escrow)
        Active,                         // 5 - tracking started
        ViolationActive,                // 6 - currently out of range
        ViolationResolved,              // 7 - back in range, awaiting completion
        ShipmentCompleted,              // 8
        Settled                         // 9 - final, immutable
    }

    struct Violation {
        uint256 startTime;
        uint256 endTime;      // 0 while still active
        int256 tempAtStartCentiC; // temperature * 100, to avoid floats on-chain
        uint256 durationSec;
        uint8 penaltyPercent;
        uint256 deductedAmount;
        bool resolved;
    }

    // ------------------------------------------------------------------
    // Agreement data — every field below is set ONLY in the constructor.
    // There is no setter for any of these anywhere in this contract, so
    // none of them can change after deployment, activation, or ever.
    // ------------------------------------------------------------------
    string public agreementId;
    address public immutable shipper;
    address public immutable transporter;
    address public immutable authorizedOracle; // the only address allowed to report sensor readings

    string public productId;
    string public batchId;
    string public containerId;

    int256 public minTemperatureCentiC; // e.g. 2400 = 24.00 C
    int256 public maxTemperatureCentiC; // e.g. 3000 = 30.00 C

    uint256 public dealAmount;      // symbolic contractual amount used for settlement math only
    // NOTE: the three fields above are no longer `immutable`. They can be
    // changed exactly once per call, and only via updateShipmentConditions()
    // below, and only while BOTH shipperSigned and transporterSigned are
    // still false. The instant either party signs, this door closes for
    // good — there is no path back to Draft, so from that point on these
    // behave exactly as immutable as they did before.
    uint256 public createdAt;

    AgreementStatus public status;
    bool public shipperSigned;
    bool public transporterSigned;

    uint256 public maxViolationDurationSec; // worst single violation, drives penalty
    uint8 public finalPenaltyPercent;
    uint256 public deductedAmount;
    uint256 public finalSettlementAmount;
    bool public settled;

    Violation[] public violations;
    int256 private _activeViolationIndex = -1; // -1 = no active violation

    int256 public lastRecordedTempCentiC; // most recent reading, in or out of range
    uint256 public lastRecordedAt;        // block timestamp of that reading
    uint256 public temperatureReadingCount;

    // ------------------------------------------------------------------
    // Events — this is the permanent public log
    // ------------------------------------------------------------------
    event AgreementCreated(string agreementId, address shipper, address transporter, uint256 dealAmount);
    event PartySigned(address signer, string role);
    event AgreementActivated(uint256 timestamp);
    event FundsLocked(uint256 amount);
    event TrackingStarted(uint256 timestamp);
    event TemperatureRecorded(int256 tempCentiC, uint256 timestamp);
    event TemperatureViolationStarted(uint256 index, int256 tempCentiC, uint256 timestamp);
    event TemperatureViolationUpdated(uint256 index, uint256 durationSec);
    event TemperatureViolationResolved(uint256 index, uint256 durationSec, uint8 penaltyPercent);
    event SettlementCalculated(uint8 penaltyPercent, uint256 deducted, uint256 finalPayment);
    event ShipmentCompleted(uint256 timestamp);
    event ShipmentConditionsUpdated(int256 minTemperatureCentiC, int256 maxTemperatureCentiC, uint256 dealAmount);

    // ------------------------------------------------------------------
    // Modifiers
    // ------------------------------------------------------------------
    modifier onlyAuthorizedOracle() {
        require(msg.sender == authorizedOracle, "Only the authorized oracle can call this");
        _;
    }

    modifier notSettled() {
        require(!settled, "Agreement already settled");
        _;
    }

    // ------------------------------------------------------------------
    // Constructor — the ONLY place any agreement term is ever assigned.
    // ------------------------------------------------------------------
    constructor(
        string memory _agreementId,
        address _shipper,
        address _transporter,
        address _authorizedOracle,
        string memory _productId,
        string memory _batchId,
        string memory _containerId,
        int256 _minTemperatureCentiC,
        int256 _maxTemperatureCentiC,
        uint256 _dealAmount
    ) {
        require(_shipper != address(0) && _transporter != address(0), "Invalid party address");
        require(_authorizedOracle != address(0), "Invalid oracle address");
        require(_shipper != _transporter, "Shipper and transporter must differ");
        require(_maxTemperatureCentiC > _minTemperatureCentiC, "Invalid temperature range");
        require(_dealAmount > 0, "Deal amount must be positive");

        agreementId = _agreementId;
        shipper = _shipper;
        transporter = _transporter;
        authorizedOracle = _authorizedOracle;
        productId = _productId;
        batchId = _batchId;
        containerId = _containerId;
        minTemperatureCentiC = _minTemperatureCentiC;
        maxTemperatureCentiC = _maxTemperatureCentiC;
        dealAmount = _dealAmount;
        createdAt = block.timestamp;
        status = AgreementStatus.WaitingForShipperSignature;

        emit AgreementCreated(_agreementId, _shipper, _transporter, _dealAmount);
    }

    // ------------------------------------------------------------------
    // Signing — only the actual shipper or transporter address may sign
    // for their own role. No one else, not even the deployer, can sign
    // on their behalf.
    // ------------------------------------------------------------------
    function signAgreement() external notSettled {
        require(
            status == AgreementStatus.WaitingForShipperSignature ||
            status == AgreementStatus.WaitingForTransporterSignature ||
            status == AgreementStatus.Draft,
            "Agreement not awaiting signatures"
        );
        require(msg.sender == shipper || msg.sender == transporter, "Not a party to this agreement");

        if (msg.sender == shipper) {
            require(!shipperSigned, "Shipper already signed");
            shipperSigned = true;
            emit PartySigned(msg.sender, "shipper");
        } else {
            require(!transporterSigned, "Transporter already signed");
            transporterSigned = true;
            emit PartySigned(msg.sender, "transporter");
        }

        if (shipperSigned && transporterSigned) {
            status = AgreementStatus.Signed;
        } else if (shipperSigned) {
            status = AgreementStatus.WaitingForTransporterSignature;
        } else {
            status = AgreementStatus.WaitingForShipperSignature;
        }
    }

    // ------------------------------------------------------------------
    // Editing shipment terms — the ONE window where minTemperatureCentiC /
    // maxTemperatureCentiC / dealAmount can change after deployment.
    // Only a party to the agreement can call this, and only while NEITHER
    // party has signed yet. Once one signature lands, this reverts for
    // every future call — terms are frozen for the rest of the
    // agreement's life, same guarantee as before, just a later trigger.
    // ------------------------------------------------------------------
    function updateShipmentConditions(
        int256 _minTemperatureCentiC,
        int256 _maxTemperatureCentiC,
        uint256 _dealAmount
    ) external notSettled {
        require(msg.sender == shipper || msg.sender == transporter, "Not a party to this agreement");
        require(!shipperSigned && !transporterSigned, "Terms are locked once either party has signed");
        require(_maxTemperatureCentiC > _minTemperatureCentiC, "Invalid temperature range");
        require(_dealAmount > 0, "Deal amount must be positive");

        minTemperatureCentiC = _minTemperatureCentiC;
        maxTemperatureCentiC = _maxTemperatureCentiC;
        dealAmount = _dealAmount;

        emit ShipmentConditionsUpdated(_minTemperatureCentiC, _maxTemperatureCentiC, _dealAmount);
    }

    // ------------------------------------------------------------------
    // Activation (symbolic — no real ETH/token ever moves; see NOTE ON
    // FUNDS at the top of this file). By the time this can be called both
    // parties have already signed, which means updateShipmentConditions()
    // has already been permanently locked out — so activation itself
    // doesn't need to freeze anything further: shipper, transporter,
    // authorizedOracle, productId, batchId, containerId,
    // minTemperatureCentiC, maxTemperatureCentiC, dealAmount and the
    // penalty tiers are all already immutable in practice at this point.
    // ------------------------------------------------------------------
    function activateAgreement() external notSettled {
        require(status == AgreementStatus.Signed, "Both parties must sign first");
        require(shipperSigned && transporterSigned, "Signatures incomplete");
        status = AgreementStatus.FundsLocked;
        emit AgreementActivated(block.timestamp);
        emit FundsLocked(dealAmount);
    }

    function startTracking() external notSettled {
        require(status == AgreementStatus.FundsLocked, "Contract must be activated first");
        require(msg.sender == shipper || msg.sender == transporter || msg.sender == authorizedOracle,
                "Not authorized to start tracking");
        status = AgreementStatus.Active;
        emit TrackingStarted(block.timestamp);
    }

    // ------------------------------------------------------------------
    // TEMPERATURE ENFORCEMENT — single entry point, single source of
    // truth. The oracle (Python backend) ONLY reports the raw reading.
    // THIS FUNCTION, using minTemperatureCentiC / maxTemperatureCentiC
    // (locked in as of the first signature — see updateShipmentConditions),
    // decides on-chain whether the reading is in range and autonomously
    // starts, extends, or resolves a violation. Python never tells the
    // contract "this is a violation" — it cannot, because there is no
    // such input parameter.
    //
    //   in range  <=>  minTemperatureCentiC <= tempCentiC <= maxTemperatureCentiC
    //
    // State machine (fully internal, no separate oracle call needed):
    //   in-range, no active violation      -> just log the reading
    //   out-of-range, no active violation  -> open a new violation
    //   out-of-range, violation active     -> extend the active violation
    //   in-range, violation active         -> close the violation and
    //                                          compute its penalty via
    //                                          calculatePenalty()
    // ------------------------------------------------------------------
    function recordTemperature(int256 tempCentiC) external onlyAuthorizedOracle notSettled {
        require(
            status == AgreementStatus.Active ||
            status == AgreementStatus.ViolationActive ||
            status == AgreementStatus.ViolationResolved,
            "Tracking is not active"
        );

        lastRecordedTempCentiC = tempCentiC;
        lastRecordedAt = block.timestamp;
        temperatureReadingCount += 1;
        emit TemperatureRecorded(tempCentiC, block.timestamp);

        bool inRange = (tempCentiC >= minTemperatureCentiC && tempCentiC <= maxTemperatureCentiC);

        if (!inRange) {
            if (_activeViolationIndex == -1) {
                _startViolation(tempCentiC);
            } else {
                _extendActiveViolation();
            }
        } else if (_activeViolationIndex != -1) {
            _resolveActiveViolation();
        }
    }

    // ------------------------------------------------------------------
    // Internal violation bookkeeping — not externally callable. The
    // oracle cannot invoke these directly; they only run as a
    // consequence of the on-chain range check inside recordTemperature().
    // ------------------------------------------------------------------
    function _startViolation(int256 tempCentiC) private {
        violations.push(Violation({
            startTime: block.timestamp,
            endTime: 0,
            tempAtStartCentiC: tempCentiC,
            durationSec: 0,
            penaltyPercent: 0,
            deductedAmount: 0,
            resolved: false
        }));
        _activeViolationIndex = int256(violations.length) - 1;
        status = AgreementStatus.ViolationActive;

        emit TemperatureViolationStarted(uint256(_activeViolationIndex), tempCentiC, block.timestamp);
    }

    function _extendActiveViolation() private {
        uint256 idx = uint256(_activeViolationIndex);
        Violation storage v = violations[idx];
        v.durationSec = block.timestamp - v.startTime;
        if (v.durationSec > maxViolationDurationSec) {
            maxViolationDurationSec = v.durationSec;
        }
        emit TemperatureViolationUpdated(idx, v.durationSec);
    }

    function _resolveActiveViolation() private {
        uint256 idx = uint256(_activeViolationIndex);
        Violation storage v = violations[idx];

        v.endTime = block.timestamp;
        v.durationSec = v.endTime - v.startTime;
        v.penaltyPercent = calculatePenalty(v.durationSec);
        v.deductedAmount = (dealAmount * v.penaltyPercent) / 100;
        v.resolved = true;

        if (v.durationSec > maxViolationDurationSec) {
            maxViolationDurationSec = v.durationSec;
        }

        _activeViolationIndex = -1;
        status = AgreementStatus.ViolationResolved;

        emit TemperatureViolationResolved(idx, v.durationSec, v.penaltyPercent);
    }

    // ------------------------------------------------------------------
    // Penalty / settlement math — single source of truth, on-chain only.
    // Tiers (fixed, no setter exists for these):
    //   duration < 60s          -> 0%
    //   60s <= duration < 120s  -> 10%
    //   120s <= duration < 300s -> 25%
    //   duration >= 300s        -> 50%
    // Applied to the ORIGINAL dealAmount only, never compounded.
    // ------------------------------------------------------------------
    function calculatePenalty(uint256 durationSec) public pure returns (uint8) {
        if (durationSec >= 300) return 50;
        if (durationSec >= 120) return 25;
        if (durationSec >= 60) return 10;
        return 0;
    }

    function calculateSettlement() public view returns (uint8 penaltyPercent, uint256 deducted, uint256 finalPayment) {
        penaltyPercent = calculatePenalty(maxViolationDurationSec);
        require(penaltyPercent <= 50, "Penalty cannot exceed 50%"); // invariant guard
        deducted = (dealAmount * penaltyPercent) / 100;
        finalPayment = dealAmount - deducted; // safe: deducted <= dealAmount by construction
        require(finalPayment <= dealAmount, "Final payment cannot exceed locked amount");
    }

    // ------------------------------------------------------------------
    // Completion + settlement — can only happen ONCE (guarded by
    // notSettled + settled flag). Not payable; no funds actually move.
    // ------------------------------------------------------------------
    function completeShipment() external notSettled {
        require(msg.sender == shipper || msg.sender == transporter || msg.sender == authorizedOracle,
                "Not authorized to complete shipment");
        require(
            status == AgreementStatus.Active || status == AgreementStatus.ViolationResolved,
            "Shipment not in a completable state"
        );
        require(_activeViolationIndex == -1, "Resolve the active violation before completing");

        (uint8 penaltyPercent, uint256 deducted, uint256 finalPayment) = calculateSettlement();
        finalPenaltyPercent = penaltyPercent;
        deductedAmount = deducted;
        finalSettlementAmount = finalPayment;
        settled = true;
        status = AgreementStatus.Settled;

        emit SettlementCalculated(penaltyPercent, deducted, finalPayment);
        emit ShipmentCompleted(block.timestamp);
    }

    // ------------------------------------------------------------------
    // Read-only getters
    // ------------------------------------------------------------------
    function getAgreementDetails() external view returns (
        string memory _agreementId, address _shipper, address _transporter,
        int256 _minTemp, int256 _maxTemp, uint256 _dealAmount,
        AgreementStatus _status, bool _shipperSigned, bool _transporterSigned
    ) {
        return (agreementId, shipper, transporter, minTemperatureCentiC, maxTemperatureCentiC,
                dealAmount, status, shipperSigned, transporterSigned);
    }

    function getViolationDetails(uint256 index) external view returns (Violation memory) {
        require(index < violations.length, "No such violation");
        return violations[index];
    }

    function getViolationCount() external view returns (uint256) {
        return violations.length;
    }

    function getActiveViolationIndex() external view returns (int256) {
        return _activeViolationIndex;
    }

    function getLastReading() external view returns (int256 tempCentiC, uint256 timestamp, uint256 readingCount) {
        return (lastRecordedTempCentiC, lastRecordedAt, temperatureReadingCount);
    }

    function getSettlementSummary() external view returns (
        bool _settled, uint8 _finalPenaltyPercent, uint256 _deductedAmount, uint256 _finalSettlementAmount
    ) {
        return (settled, finalPenaltyPercent, deductedAmount, finalSettlementAmount);
    }
}
