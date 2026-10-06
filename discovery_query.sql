SELECT
  signature,
  block_timestamp
FROM `solana-data-sandbox.crypto_solana_mainnet_us.Transactions`
WHERE block_timestamp BETWEEN TIMESTAMP('2026-06-01') AND TIMESTAMP('2026-09-22')
  AND status = 'Success'
  AND EXISTS (
    SELECT 1 FROM UNNEST(accounts) AS a
    WHERE a.pubkey IN ('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P', 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA')
  )
ORDER BY block_timestamp
