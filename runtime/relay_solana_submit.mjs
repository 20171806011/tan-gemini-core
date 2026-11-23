#!/usr/bin/env node
/**
 * TAN Relay Solana -> Base USDC Submitter (Dry-Run by Default)
 * ============================================================
 *
 * Compiles, independently verifies, simulates (--dry-run, default), and
 * optionally submits (--execute) a Relay Protocol Solana VersionedTransaction
 * produced by `runtime/relay_bridge.py --save-quote`.
 *
 * Safety invariants verified BEFORE signing or sending:
 *   1. Destination chainId === 8453 (Base Mainnet)
 *   2. Destination token === 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 (Base USDC)
 *   3. Recipient === 0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5 (hardcoded treasury)
 *   4. Implied fee <= maxFeeUsd (default $0.15)
 *   5. Every Solana instruction programId is in the strict Relay allowlist
 *   6. Depository instruction bytes[8..16] (LE u64) === exact micro-USDC amount
 *   7. Loaded Solana keypair public key matches expected signer & instruction signer
 *   8. NEVER broadcasts unless --execute is explicitly passed
 */

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const BASE_CHAIN_ID = 8453;
const BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const DEFAULT_BASE_TREASURY = "0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5";
const DEFAULT_SOLANA_WALLET = "GJhyghToEGupadoHbPVZyXMzreQb5wHSy4AvavvraRbU";
const RELAY_DEPOSITORY_PROGRAM = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2";
const ALLOWED_PROGRAMS = new Set([
  RELAY_DEPOSITORY_PROGRAM,
  "ComputeBudget111111111111111111111111111111",
  "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
]);

function loadSolanaWeb3() {
  const candidates = [
    process.env.TAN_TOOLS_PACKAGE_JSON,
    "/home/antigravity/tan-tools/package.json",
    path.resolve(process.cwd(), "package.json"),
  ].filter(Boolean);
  for (const pkgPath of candidates) {
    if (fs.existsSync(pkgPath)) {
      try {
        const req = createRequire(pkgPath);
        return req("@solana/web3.js");
      } catch {
        // try next candidate
      }
    }
  }
  const req = createRequire(import.meta.url);
  return req("@solana/web3.js");
}

function parseArgs(argv) {
  const opts = {
    quotePath: "",
    keypairPath: "/home/antigravity/.tan-secrets/makefun-keypair.json",
    rpcUrl: "https://api.mainnet-beta.solana.com",
    expectedRecipient: DEFAULT_BASE_TREASURY,
    expectedWallet: DEFAULT_SOLANA_WALLET,
    maxFeeUsd: 0.15,
    execute: false,
    pollSeconds: 60,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--quote") opts.quotePath = argv[++i] || "";
    else if (a === "--keypair") opts.keypairPath = argv[++i] || "";
    else if (a === "--rpc") opts.rpcUrl = argv[++i] || "";
    else if (a === "--max-fee-usd") opts.maxFeeUsd = Number(argv[++i]);
    else if (a === "--poll-seconds") opts.pollSeconds = Number(argv[++i]);
    else if (a === "--execute") opts.execute = true;
    else if (a === "--dry-run") opts.execute = false;
    else if (a === "--help" || a === "-h") {
      console.log(
        "Usage: node runtime/relay_solana_submit.mjs --quote <file.json> [--keypair <kp.json>] [--dry-run | --execute]"
      );
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${a}`);
    }
  }
  if (!opts.quotePath) {
    throw new Error("Missing required --quote <path> argument");
  }
  return opts;
}

export function verifyQuoteBundle(bundle, opts = {}) {
  const expectedRecipient = (opts.expectedRecipient || DEFAULT_BASE_TREASURY).toLowerCase();
  const expectedWallet = opts.expectedWallet || DEFAULT_SOLANA_WALLET;
  const maxFeeUsd = Number.isFinite(opts.maxFeeUsd) ? opts.maxFeeUsd : 0.15;

  const quote = bundle.quote || bundle;
  const summary = bundle.summary || {};
  const details = quote.details || {};
  const curIn = details.currencyIn || {};
  const curOut = details.currencyOut || {};
  const outMeta = curOut.currency || {};

  const outChain = Number(outMeta.chainId || 0);
  const outAddr = String(outMeta.address || "").toLowerCase();
  const recipient = String(details.recipient || "").toLowerCase();
  const amountInUsdc = Number(summary.amount_in_usdc ?? curIn.amountFormatted ?? 0);
  const amountOutUsdc = Number(curOut.amountFormatted ?? 0);

  if (outChain !== BASE_CHAIN_ID) {
    throw new Error(`Unexpected destination chainId: ${outChain}`);
  }
  if (outAddr !== BASE_USDC_ADDRESS.toLowerCase()) {
    throw new Error(`Unexpected destination token: ${outAddr}`);
  }
  if (recipient && recipient !== expectedRecipient) {
    throw new Error(`Recipient mismatch: ${recipient} !== ${expectedRecipient}`);
  }
  if (!(amountInUsdc > 0) || !(amountOutUsdc > 0)) {
    throw new Error(`Invalid input/output amounts: in=${amountInUsdc}, out=${amountOutUsdc}`);
  }
  const impliedFee = Number((amountInUsdc - amountOutUsdc).toFixed(6));
  if (impliedFee > maxFeeUsd) {
    throw new Error(`Implied fee $${impliedFee} exceeds cap $${maxFeeUsd}`);
  }

  const step0 = (quote.steps || [])[0];
  const item0 = (step0?.items || [])[0];
  const txData = item0?.data || {};
  const instructions = txData.instructions || [];
  if (!instructions.length) {
    throw new Error("Quote contains no Solana instructions");
  }

  const expectedMicro = BigInt(Math.round(amountInUsdc * 1_000_000));
  let foundDepository = false;

  for (let i = 0; i < instructions.length; i++) {
    const ins = instructions[i];
    const prog = String(ins.programId || "");
    if (!ALLOWED_PROGRAMS.has(prog)) {
      throw new Error(`Disallowed Solana programId in instruction[${i}]: ${prog}`);
    }
    if (prog === RELAY_DEPOSITORY_PROGRAM) {
      const keys = ins.keys || [];
      const hasSigner = keys.some((k) => k.pubkey === expectedWallet && k.isSigner === true);
      const hasUsdcMint = keys.some((k) => k.pubkey === SOLANA_USDC_MINT);
      if (!hasSigner) {
        throw new Error(`Expected signer ${expectedWallet} missing from depository instruction`);
      }
      if (!hasUsdcMint) {
        throw new Error(`USDC mint ${SOLANA_USDC_MINT} missing from depository instruction`);
      }
      const raw = Buffer.from(String(ins.data || ""), "hex");
      if (raw.length < 16) {
        throw new Error(`Depository instruction data too short (${raw.length} bytes)`);
      }
      const encodedMicro = raw.readBigUInt64LE(8);
      if (encodedMicro !== expectedMicro) {
        throw new Error(`Amount mismatch in instruction bytes: ${encodedMicro} !== ${expectedMicro}`);
      }
      foundDepository = true;
    }
  }
  if (!foundDepository) {
    throw new Error("Missing Relay depository instruction");
  }

  const endpoint = String(item0?.check?.endpoint || "");
  const requestId = endpoint.includes("requestId=") ? endpoint.split("requestId=")[1] : "";

  return {
    amountInUsdc,
    amountOutUsdc,
    impliedFee,
    expectedMicro: expectedMicro.toString(),
    instructions,
    altAddresses: txData.addressLookupTableAddresses || [],
    requestId,
  };
}

async function pollRelayStatus(requestId, maxSeconds) {
  if (!requestId) return { status: "unknown_no_request_id" };
  const deadline = Date.now() + maxSeconds * 1000;
  let last = { status: "pending" };
  while (Date.now() < deadline) {
    const resp = await fetch(`https://api.relay.link/intents/status?requestId=${encodeURIComponent(requestId)}`, {
      headers: { "User-Agent": "tan-relay-submit/1.0" },
    });
    if (resp.ok) {
      last = await resp.json();
      if (["success", "refund", "failure"].includes(String(last.status || "").toLowerCase())) {
        return last;
      }
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  return last;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const bundle = JSON.parse(fs.readFileSync(opts.quotePath, "utf8"));
  const verified = verifyQuoteBundle(bundle, opts);

  const web3 = loadSolanaWeb3();
  const { Connection, Keypair, PublicKey, TransactionInstruction, TransactionMessage, VersionedTransaction } = web3;

  const secretRaw = JSON.parse(fs.readFileSync(opts.keypairPath, "utf8"));
  if (!Array.isArray(secretRaw) || secretRaw.length !== 64) {
    throw new Error("Invalid Solana keypair JSON format (expected 64-byte integer array)");
  }
  const payer = await Keypair.fromSecretKey(Uint8Array.from(secretRaw));
  const payerPub = payer.publicKey.toBase58();
  if (payerPub !== opts.expectedWallet) {
    throw new Error(`Keypair pubkey ${payerPub} !== expected wallet ${opts.expectedWallet}`);
  }

  const connection = new Connection(opts.rpcUrl, "confirmed");
  const solLamportsRaw = await connection.getBalance(payer.publicKey);
  const solLamports = Number(solLamportsRaw);
  const tokenAccounts = await connection.getParsedTokenAccountsByOwner(payer.publicKey, {
    mint: new PublicKey(SOLANA_USDC_MINT),
  });
  let usdcBalance = 0;
  for (const acc of tokenAccounts.value) {
    usdcBalance += Number(acc.account.data?.parsed?.info?.tokenAmount?.uiAmount || 0);
  }

  const altAccounts = [];
  for (const addr of verified.altAddresses) {
    const res = await connection.getAddressLookupTable(new PublicKey(addr));
    if (!res.value) {
      throw new Error(`Failed to fetch AddressLookupTable ${addr}`);
    }
    altAccounts.push(res.value);
  }

  const txInstructions = verified.instructions.map(
    (ins) =>
      new TransactionInstruction({
        programId: new PublicKey(ins.programId),
        keys: (ins.keys || []).map((k) => ({
          pubkey: new PublicKey(k.pubkey),
          isSigner: Boolean(k.isSigner),
          isWritable: Boolean(k.isWritable),
        })),
        data: Buffer.from(String(ins.data || ""), "hex"),
      })
  );

  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  const messageV0 = new TransactionMessage({
    payerKey: payer.publicKey,
    recentBlockhash: blockhash,
    instructions: txInstructions,
  }).compileToV0Message(altAccounts);

  const tx = new VersionedTransaction(messageV0);
  await tx.sign([payer]);
  const serialized = tx.serialize();
  const jsonReplacer = (_, v) => (typeof v === "bigint" ? v.toString() : v);

  if (!opts.execute) {
    const sim = await connection.simulateTransaction(tx, { sigVerify: true });
    console.log(
      JSON.stringify(
        {
          mode: "dry_run",
          verified_safety: true,
          signer: payerPub,
          recipient_base: opts.expectedRecipient,
          amount_in_usdc: verified.amountInUsdc,
          amount_out_usdc: verified.amountOutUsdc,
          implied_fee_usd: verified.impliedFee,
          verified_micro_usdc: verified.expectedMicro,
          request_id: verified.requestId,
          tx_bytes: serialized.length,
          alt_count: altAccounts.length,
          live_sol_balance: solLamports / 1e9,
          live_usdc_balance: usdcBalance,
          simulation_err: sim.value.err,
          units_consumed: sim.value.unitsConsumed ?? null,
          logs: (sim.value.logs || []).slice(0, 12),
        },
        jsonReplacer,
        2
      )
    );
    return;
  }

  if (solLamports < 1_000_000) {
    throw new Error(`Insufficient SOL for gas: ${solLamports / 1e9} SOL (need >= 0.001 SOL)`);
  }
  if (usdcBalance + 1e-6 < verified.amountInUsdc) {
    throw new Error(`Insufficient Solana USDC: ${usdcBalance} < ${verified.amountInUsdc}`);
  }

  const signature = await connection.sendRawTransaction(serialized, {
    skipPreflight: false,
    maxRetries: 3,
  });
  const confirmation = await connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    "confirmed"
  );
  if (confirmation.value.err) {
    throw new Error(`Solana tx failed on-chain: ${JSON.stringify(confirmation.value.err, jsonReplacer)}`);
  }

  const relayStatus = await pollRelayStatus(verified.requestId, opts.pollSeconds);
  console.log(
    JSON.stringify(
      {
        mode: "execute",
        ok: String(relayStatus.status || "").toLowerCase() === "success",
        solana_signature: signature,
        request_id: verified.requestId,
        amount_in_usdc: verified.amountInUsdc,
        amount_out_usdc: verified.amountOutUsdc,
        relay_status: relayStatus,
      },
      jsonReplacer,
      2
    )
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(JSON.stringify({ ok: false, error: String(err?.message || err) }));
    process.exit(1);
  });
}
