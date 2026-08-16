const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

/**
 * scripts/deploy.js
 * ----------------------------------------------------------------------
 * Deploys ColdChainAgreement.sol to whatever network you pass via
 * --network (localhost = your `npx hardhat node` instance on :8545).
 *
 * Demo account layout (Hardhat's default 20 funded test accounts):
 *   signers[0] = deployer (unused as a party, just pays deploy gas)
 *   signers[1] = shipper
 *   signers[2] = transporter
 *   signers[3] = authorizedOracle   <-- put this address's private key
 *                                       into oracle_bridge.py's
 *                                       ORACLE_PRIVATE_KEY
 *
 * After deploying, this script writes:
 *   - contract_abi.json   (ABI array only — what web3.py / oracle_bridge.py needs)
 *   - deployment.json     (address + all four account addresses, for reference)
 * both at the project root, so you can copy contract_abi.json next to
 * oracle_bridge.py and paste the printed address into CONTRACT_ADDRESS.
 * ----------------------------------------------------------------------
 */
async function main() {
  const [deployer, shipper, transporter, oracle] = await hre.ethers.getSigners();

  console.log("Deploying with account:", deployer.address);
  console.log("  shipper         :", shipper.address);
  console.log("  transporter     :", transporter.address);
  console.log("  authorizedOracle:", oracle.address);

  const Factory = await hre.ethers.getContractFactory("ColdChainAgreement");

  const contract = await Factory.deploy(
    "CC-001",                 // agreementId
    shipper.address,          // shipper
    transporter.address,      // transporter
    oracle.address,           // authorizedOracle
    "VAC-2026-001",           // productId
    "VAC-2026-001",           // batchId
    "UNIT-DEMO-01",           // containerId
    2400,                     // minTemperatureCentiC  = 24.00 C
    3000,                     // maxTemperatureCentiC  = 30.00 C
    hre.ethers.parseEther("1") // dealAmount, in wei
  );

  await contract.waitForDeployment();
  const address = await contract.getAddress();

  console.log("ColdChainAgreement deployed at:", address);

  // ---- Export ABI for the Python oracle bridge ----------------------
  const artifact = await hre.artifacts.readArtifact("ColdChainAgreement");
  const abiPath = path.join(__dirname, "..", "contract_abi.json");
  fs.writeFileSync(abiPath, JSON.stringify(artifact.abi, null, 2));
  console.log("ABI written to:", abiPath);

  // ---- Export deployment info for reference --------------------------
  const deploymentInfo = {
    contractAddress: address,
    deployer: deployer.address,
    shipper: shipper.address,
    transporter: transporter.address,
    authorizedOracle: oracle.address,
    network: hre.network.name,
    deployedAt: new Date().toISOString(),
  };
  const deploymentPath = path.join(__dirname, "..", "deployment.json");
  fs.writeFileSync(deploymentPath, JSON.stringify(deploymentInfo, null, 2));
  console.log("Deployment info written to:", deploymentPath);

  console.log("\nNext steps:");
  console.log("  1. Copy contract_abi.json next to oracle_bridge.py");
  console.log(`  2. Set CONTRACT_ADDRESS = "${address}" in oracle_bridge.py`);
  console.log("  3. Set ORACLE_PRIVATE_KEY to the private key Hardhat printed");
  console.log(`     for account ${oracle.address} when you ran 'npx hardhat node'`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
