require("@nomicfoundation/hardhat-toolbox");

/**
 * hardhat.config.js
 * ----------------------------------------------------------------------
 * - `hardhat` network: in-process chain used by `npx hardhat test`.
 * - `localhost` network: points at a node started separately with
 *   `npx hardhat node` (or Ganache) on 127.0.0.1:8545 — this is the
 *   network oracle_bridge.py's RPC_URL talks to. Deploy with:
 *     npx hardhat run scripts/deploy.js --network localhost
 * ----------------------------------------------------------------------
 */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200,
      },
    },
  },
  networks: {
    hardhat: {
      chainId: 31337,
    },
    localhost: {
      url: "http://127.0.0.1:8545",
      chainId: 31337,
    },
  },
  paths: {
    sources: "./contracts",
    artifacts: "./artifacts",
    cache: "./cache",
  },
};
