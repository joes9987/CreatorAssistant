import assert from "node:assert/strict";
import test from "node:test";

import * as blobSdk from "@vercel/blob";

import { handleIgMedia } from "../api/ig-media.js";

const TOKEN = "test-storage-token";
const CLIP_ID = "11111111-1111-4111-8111-111111111111";
const PATHNAME = `ig-reels/${CLIP_ID}.mp4`;
const SIGNING_SECRET = "super-secret-signing-key";

function mockRes() {
  return {
    statusCode: 0,
    body: null,
    headers: {},
    setHeader(name, value) {
      this.headers[name] = value;
    },
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(payload) {
      this.body = payload;
      return this;
    },
  };
}

function fakeBlob() {
  const calls = [];
  return {
    calls,
    async issueSignedToken(options) {
      calls.push(["issue", options]);
      return {
        delegationToken: "deleg",
        clientSigningToken: SIGNING_SECRET,
        validUntil: options.validUntil,
      };
    },
    async presignUrl(_token, options) {
      calls.push(["presign", options.operation, options]);
      if (options.operation === "put") {
        return {
          presignedUrl: `https://vercel.com/api/blob/?pathname=${encodeURIComponent(options.pathname)}&sig=put`,
        };
      }
      return {
        presignedUrl: `https://store123.private.blob.vercel-storage.com/${options.pathname}?sig=get`,
      };
    },
    parseStoreIdFromDelegationToken() {
      return "store123";
    },
    async del(pathname) {
      calls.push(["del", pathname]);
    },
  };
}

function env(extra = {}) {
  return { IG_STORAGE_TOKEN: TOKEN, ...extra };
}

async function post(body, { blob = fakeBlob(), headers, environment } = {}) {
  const res = mockRes();
  await handleIgMedia(
    {
      method: "POST",
      headers: headers ?? { authorization: `Bearer ${TOKEN}` },
      body,
    },
    res,
    {
      blob,
      env: environment ?? env(),
      now: () => 1_700_000_000_000,
      randomId: () => CLIP_ID,
    },
  );
  return { res, blob };
}

test("installed Blob SDK exports the hosting helpers", () => {
  assert.equal(typeof blobSdk.issueSignedToken, "function");
  assert.equal(typeof blobSdk.presignUrl, "function");
  assert.equal(typeof blobSdk.del, "function");
  assert.equal(typeof blobSdk.parseStoreIdFromDelegationToken, "function");
});

test("rejects non-POST", async () => {
  const res = mockRes();
  await handleIgMedia({ method: "GET", headers: {}, body: {} }, res, { env: env(), blob: fakeBlob() });
  assert.equal(res.statusCode, 405);
  assert.equal(res.headers.Allow, "POST");
});

test("reports a missing server token instead of accepting uploads", async () => {
  const blob = fakeBlob();
  const { res } = await post(
    { op: "create", contentType: "video/mp4", contentLength: 10 },
    { blob, environment: {} },
  );
  assert.equal(res.statusCode, 500);
  assert.match(res.body.error, /IG_STORAGE_TOKEN/);
  assert.equal(blob.calls.length, 0);
});

test("rejects a missing or wrong bearer token before touching Blob", async () => {
  const blob = fakeBlob();
  const missing = await post(
    { op: "create", contentType: "video/mp4", contentLength: 10 },
    { blob, headers: {} },
  );
  const wrong = await post(
    { op: "create", contentType: "video/mp4", contentLength: 10 },
    { blob, headers: { authorization: "Bearer nope" } },
  );
  assert.equal(missing.res.statusCode, 401);
  assert.equal(wrong.res.statusCode, 401);
  assert.equal(blob.calls.length, 0);
});

test("private create issues a scoped put+get token and does not leak the signing secret", async () => {
  const blob = fakeBlob();
  const { res } = await post(
    { op: "create", contentType: "video/mp4", contentLength: 2048 },
    { blob },
  );
  assert.equal(res.statusCode, 200);
  assert.equal(res.headers["Cache-Control"], "no-store");
  assert.equal(res.body.pathname, PATHNAME);
  assert.equal(res.body.access, "private");
  assert.equal(res.body.contentType, "video/mp4");
  assert.match(res.body.uploadUrl, /^https:\/\/vercel\.com\/api\/blob\//);
  assert.match(res.body.videoUrl, /^https:\/\/store123\.private\.blob\.vercel-storage\.com\//);
  assert.equal(JSON.stringify(res.body).includes(SIGNING_SECRET), false);

  const issue = blob.calls.find((call) => call[0] === "issue");
  assert.deepEqual(issue[1].operations, ["put", "get"]);
  assert.equal(issue[1].pathname, PATHNAME);
  assert.deepEqual(issue[1].allowedContentTypes, ["video/mp4"]);
  assert.equal(issue[1].maximumSizeInBytes, 512 * 1024 * 1024);

  const put = blob.calls.find((call) => call[0] === "presign" && call[1] === "put");
  assert.equal(put[2].addRandomSuffix, false);
  assert.equal(put[2].allowOverwrite, false);
  assert.equal(put[2].access, "private");
  const get = blob.calls.find((call) => call[0] === "presign" && call[1] === "get");
  assert.equal(get[2].useCache, false);
  assert.equal(get[2].validUntil - put[2].validUntil, 15 * 60 * 1000);
});

test("public create returns a plain blob URL and only a put token", async () => {
  const blob = fakeBlob();
  const { res } = await post(
    { op: "create", contentType: "video/mp4", contentLength: 100 },
    { blob, environment: env({ IG_BLOB_ACCESS: "public" }) },
  );
  assert.equal(res.statusCode, 200);
  assert.equal(
    res.body.videoUrl,
    `https://store123.public.blob.vercel-storage.com/${PATHNAME}`,
  );
  assert.equal(blob.calls[0][1].operations.includes("get"), false);
  assert.equal(blob.calls.some((call) => call[1] === "get"), false);
});

test("rejects non-mp4, bad sizes, and unknown ops", async () => {
  const type = await post({ op: "create", contentType: "image/png", contentLength: 10 });
  const size = await post({ op: "create", contentType: "video/mp4", contentLength: 0 });
  const huge = await post({
    op: "create",
    contentType: "video/mp4",
    contentLength: 512 * 1024 * 1024 + 1,
  });
  const op = await post({ op: "list" });
  assert.equal(type.res.statusCode, 400);
  assert.match(type.res.body.error, /video\/mp4/);
  assert.equal(size.res.statusCode, 400);
  assert.equal(huge.res.statusCode, 400);
  assert.match(huge.res.body.error, /byte limit/);
  assert.equal(op.res.statusCode, 400);
  assert.equal(type.blob.calls.length, 0);
});

test("redacts the signing secret if Blob throws after the token is issued", async () => {
  const blob = fakeBlob();
  blob.presignUrl = async (token) => {
    throw new Error(`upload failed ${token.clientSigningToken}`);
  };
  const { res } = await post(
    { op: "create", contentType: "video/mp4", contentLength: 10 },
    { blob },
  );
  assert.equal(res.statusCode, 502);
  assert.equal(JSON.stringify(res.body).includes(SIGNING_SECRET), false);
  assert.match(res.body.error, /\[redacted\]/);
});

test("delete only removes a generated ig-reels pathname", async () => {
  const blob = fakeBlob();
  const ok = await post({ op: "delete", pathname: PATHNAME }, { blob });
  assert.equal(ok.res.statusCode, 200);
  assert.equal(ok.res.body.deleted, true);
  assert.deepEqual(blob.calls, [["del", PATHNAME]]);

  for (const pathname of [
    "ig-reels/../secret.mp4",
    "other/11111111-1111-4111-8111-111111111111.mp4",
    "ig-reels/not-a-uuid.mp4",
    PATHNAME + "/extra",
  ]) {
    const blocked = fakeBlob();
    const res = await post({ op: "delete", pathname }, { blob: blocked });
    assert.equal(res.res.statusCode, 400, pathname);
    assert.equal(blocked.calls.length, 0);
  }
});

test("delete of an already-removed clip is success", async () => {
  const blob = fakeBlob();
  blob.del = async () => {
    const err = new Error("The requested blob does not exist");
    err.name = "BlobNotFoundError";
    throw err;
  };
  const { res } = await post({ op: "delete", pathname: PATHNAME }, { blob });
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.deleted, true);
});

test("bad IG_BLOB_ACCESS fails closed", async () => {
  const blob = fakeBlob();
  const { res } = await post(
    { op: "create", contentType: "video/mp4", contentLength: 10 },
    { blob, environment: env({ IG_BLOB_ACCESS: "world" }) },
  );
  assert.equal(res.statusCode, 500);
  assert.match(res.body.error, /IG_BLOB_ACCESS/);
  assert.equal(blob.calls.length, 0);
});
