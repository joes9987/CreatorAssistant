/**
 * Instagram clip hosting for CreatorAssistant.
 *
 * The desktop app cannot hold a Blob read-write token: that credential can list,
 * read, and delete the whole store. This route runs on the Vercel project that
 * already serves the OAuth callback. It checks a separate shared secret
 * (IG_STORAGE_TOKEN), then issues a short-lived presigned upload for one
 * unguessable ig-reels/<uuid>.mp4 object and deletes that object later.
 *
 * Video bytes are PUT directly to Blob. They do not pass through this function.
 *
 * Required env (Vercel project, not the desktop app):
 *   IG_STORAGE_TOKEN   long random secret; the same value as instagram.storage.token
 *   Blob store connected to the project (OIDC / BLOB_STORE_ID, or BLOB_READ_WRITE_TOKEN)
 * Optional:
 *   IG_BLOB_ACCESS     "private" (default) or "public" — must match the store
 *   IG_BLOB_MAX_BYTES  positive integer cap (default 512 MiB)
 */

import { timingSafeEqual } from "node:crypto";

import {
  del,
  issueSignedToken,
  parseStoreIdFromDelegationToken,
  presignUrl,
} from "@vercel/blob";

const PREFIX = "ig-reels/";
const PATHNAME_RE =
  /^ig-reels\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$/;
const PUT_TTL_MS = 15 * 60 * 1000;
const GET_TTL_MS = 30 * 60 * 1000;
const DEFAULT_MAX_BYTES = 512 * 1024 * 1024;

function sendJson(res, status, payload) {
  res.setHeader("Content-Type", "application/json");
  res.setHeader("Cache-Control", "no-store");
  if (typeof res.status === "function" && typeof res.json === "function") {
    res.status(status).json(payload);
    return;
  }
  res.statusCode = status;
  res.end(JSON.stringify(payload));
}

function bearerToken(req) {
  const headers = req.headers || {};
  const header = headers.authorization || headers.Authorization || "";
  const match = /^Bearer\s+(\S+)\s*$/i.exec(String(header));
  return match ? match[1] : "";
}

function tokensMatch(provided, expected) {
  const left = Buffer.from(provided);
  const right = Buffer.from(expected);
  if (left.length === 0 || left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

function readJsonBody(req) {
  const body = req.body;
  if (body == null || body === "") {
    throw new Error("Expected a JSON body.");
  }
  if (Buffer.isBuffer(body)) return JSON.parse(body.toString("utf8"));
  if (typeof body === "string") return JSON.parse(body);
  if (typeof body === "object") return body;
  throw new Error("Expected a JSON body.");
}

function publicError(err, secret) {
  let message = err instanceof Error ? err.message : "Clip storage request failed.";
  if (secret) message = message.split(secret).join("[redacted]");
  return message;
}

function maxBytes(env) {
  const raw = env.IG_BLOB_MAX_BYTES;
  if (raw == null || String(raw).trim() === "") return DEFAULT_MAX_BYTES;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error("IG_BLOB_MAX_BYTES must be a positive integer.");
  }
  return parsed;
}

function blobAccess(env) {
  const access = String(env.IG_BLOB_ACCESS || "private").trim().toLowerCase();
  if (access !== "private" && access !== "public") {
    throw new Error('IG_BLOB_ACCESS must be "private" or "public".');
  }
  return access;
}

function assertUploadUrl(url) {
  const parsed = new URL(url);
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "vercel.com" ||
    !parsed.pathname.startsWith("/api/blob")
  ) {
    throw new Error("Blob storage returned an unexpected upload URL.");
  }
  return url;
}

function assertVideoUrl(url) {
  const parsed = new URL(url);
  const host = parsed.hostname || "";
  if (parsed.protocol !== "https:" || !host.endsWith(".blob.vercel-storage.com")) {
    throw new Error("Blob storage returned an unexpected video URL.");
  }
  return url;
}

function isMissingBlob(err) {
  const name = err && err.name;
  const message = (err && err.message) || "";
  return name === "BlobNotFoundError" || /does not exist/i.test(message);
}

async function createSlot(body, blob, env, now, randomId) {
  if (body.contentType !== "video/mp4") {
    return { status: 400, body: { error: "Only video/mp4 clips can be hosted." } };
  }
  const length = body.contentLength;
  if (typeof length !== "number" || !Number.isInteger(length) || length <= 0) {
    return { status: 400, body: { error: "contentLength must be a positive integer." } };
  }
  const limit = maxBytes(env);
  if (length > limit) {
    return {
      status: 400,
      body: { error: `Clip is ${length} bytes, which is over the ${limit} byte limit.` },
    };
  }

  const access = blobAccess(env);
  const pathname = `${PREFIX}${randomId()}.mp4`;
  if (!PATHNAME_RE.test(pathname)) {
    return { status: 500, body: { error: "Could not allocate a clip pathname." } };
  }

  const issuedAt = now();
  const getUntil = issuedAt + GET_TTL_MS;
  const putUntil = issuedAt + PUT_TTL_MS;
  let signingSecret = "";
  try {
    const issued = await blob.issueSignedToken({
      pathname,
      operations: access === "private" ? ["put", "get"] : ["put"],
      allowedContentTypes: ["video/mp4"],
      maximumSizeInBytes: limit,
      validUntil: access === "private" ? getUntil : putUntil,
    });
    signingSecret = issued.clientSigningToken || "";
    const put = await blob.presignUrl(issued, {
      operation: "put",
      pathname,
      access,
      allowedContentTypes: ["video/mp4"],
      maximumSizeInBytes: limit,
      addRandomSuffix: false,
      allowOverwrite: false,
      validUntil: putUntil,
      cacheControlMaxAge: 60,
    });

    let videoUrl;
    if (access === "public") {
      const storeId = blob.parseStoreIdFromDelegationToken(issued.delegationToken);
      if (!/^[A-Za-z0-9]+$/.test(storeId || "")) {
        throw new Error("Blob storage returned an unexpected store id.");
      }
      videoUrl = `https://${storeId}.public.blob.vercel-storage.com/${pathname}`;
    } else {
      const get = await blob.presignUrl(issued, {
        operation: "get",
        pathname,
        access: "private",
        validUntil: getUntil,
        useCache: false,
      });
      videoUrl = get.presignedUrl;
    }

    return {
      status: 200,
      body: {
        uploadUrl: assertUploadUrl(put.presignedUrl),
        videoUrl: assertVideoUrl(videoUrl),
        pathname,
        contentType: "video/mp4",
        access,
      },
    };
  } catch (err) {
    return { status: 502, body: { error: publicError(err, signingSecret) } };
  }
}

async function deleteSlot(body, blob) {
  const pathname = body.pathname;
  if (typeof pathname !== "string" || !PATHNAME_RE.test(pathname)) {
    return { status: 400, body: { error: "pathname must be an ig-reels clip id." } };
  }
  try {
    await blob.del(pathname);
  } catch (err) {
    if (isMissingBlob(err)) return { status: 200, body: { deleted: true } };
    return { status: 502, body: { error: publicError(err, "") } };
  }
  return { status: 200, body: { deleted: true } };
}

export async function handleIgMedia(req, res, deps = {}) {
  const blob = deps.blob || {
    del,
    issueSignedToken,
    parseStoreIdFromDelegationToken,
    presignUrl,
  };
  const env = deps.env || process.env;
  const now = deps.now || Date.now;
  const randomId = deps.randomId || (() => crypto.randomUUID());

  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return sendJson(res, 405, { error: "Method not allowed." });
  }

  const expected = String(env.IG_STORAGE_TOKEN || "").trim();
  if (!expected) {
    return sendJson(res, 500, {
      error: "Instagram storage is not configured on the server. Set IG_STORAGE_TOKEN.",
    });
  }
  if (!tokensMatch(bearerToken(req), expected)) {
    return sendJson(res, 401, { error: "Invalid storage token." });
  }

  let body;
  try {
    body = readJsonBody(req);
  } catch {
    return sendJson(res, 400, { error: "Expected a JSON body." });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return sendJson(res, 400, { error: "Expected a JSON object." });
  }

  try {
    if (body.op === "create") {
      const result = await createSlot(body, blob, env, now, randomId);
      return sendJson(res, result.status, result.body);
    }
    if (body.op === "delete") {
      const result = await deleteSlot(body, blob);
      return sendJson(res, result.status, result.body);
    }
    return sendJson(res, 400, { error: 'op must be "create" or "delete".' });
  } catch (err) {
    return sendJson(res, 500, { error: publicError(err, "") });
  }
}

export default function handler(req, res) {
  return handleIgMedia(req, res);
}
