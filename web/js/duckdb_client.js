/* Lazy-loaded Browser DuckDB-Wasm SQL Engine (Step 3: Tier 2 Full 454K-Row Queries)
 *
 * Loads `@duckdb/duckdb-wasm` from jsDelivr ONLY when a question requires exact
 * ad-hoc SQL over all 454,051 canonical preprints, 3,034 clinical trials, or
 * 399 NIH iCite translational papers. Queries static ZSTD Parquet files via
 * HTTP Range requests (same-origin GitHub Pages by default, or Hugging Face Hub).
 */

(function () {
  const DUCKDB_ESM_URL = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";
  const SQL_FORBIDDEN_RE =
    /\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|COPY|EXPORT|IMPORT|PRAGMA|LOAD|INSTALL|CALL|EXECUTE|SET|RESET|VACUUM|CHECKPOINT|USE|SECRET|READ_CSV|READ_CSV_AUTO|READ_JSON|READ_JSON_AUTO|READ_PARQUET|READ_TEXT|READ_BLOB|PARQUET_SCAN|CSV_SCAN|SQLITE_SCAN|GLOB|HTTP_GET)\b/i;

  let initPromise = null;
  let dbInstance = null;
  let connInstance = null;

  function getParquetBaseUrl() {
    const cfg = (window.BIOMEDRXIV_CHAT_CONFIG && window.BIOMEDRXIV_CHAT_CONFIG.parquetBaseUrl) || "./data/parquet";
    const resolved = new URL(cfg.replace(/\/+$/, "") + "/", window.location.href).href;
    return resolved.replace(/\/+$/, "");
  }

  function validateSql(rawSql) {
    if (!rawSql || typeof rawSql !== "string") {
      return { ok: false, error: "Empty SQL query." };
    }
    let sql = rawSql
      .replace(/^\s*```(?:sql)?\s*/i, "")
      .replace(/\s*```\s*$/, "")
      .trim()
      .replace(/;\s*$/, "")
      .trim();
    if (!sql) return { ok: false, error: "Empty SQL query." };
    if (!/^(SELECT|WITH)\b/i.test(sql)) {
      return { ok: false, error: "Only read-only SELECT / WITH queries are allowed." };
    }
    if (sql.includes(";")) {
      return { ok: false, error: "Multiple SQL statements are not allowed." };
    }
    if (SQL_FORBIDDEN_RE.test(sql)) {
      return { ok: false, error: "Query contains a forbidden SQL keyword or file function." };
    }
    if (!/\bLIMIT\s+\d+\b/i.test(sql)) {
      sql = `${sql}\nLIMIT 100`;
    }
    return { ok: true, sql };
  }

  function normalizeArrowValue(val) {
    if (val === null || val === undefined) return null;
    if (typeof val === "bigint") {
      const asNum = Number(val);
      return Number.isSafeInteger(asNum) ? asNum : val.toString();
    }
    if (val instanceof Date) {
      return Number.isNaN(val.getTime()) ? null : val.toISOString().slice(0, 10);
    }
    if (typeof val === "object") {
      // Handle Arrow Decimal / TypedArray / Uint32Array gracefully
      if (typeof val.toString === "function" && val.toString !== Object.prototype.toString) {
        return val.toString();
      }
      try {
        return JSON.stringify(val);
      } catch {
        return String(val);
      }
    }
    return val;
  }

  async function init(onStatus) {
    if (connInstance) return connInstance;
    if (initPromise) return initPromise;

    initPromise = (async () => {
      if (typeof onStatus === "function") onStatus("Loading DuckDB-Wasm engine (first query only)…");
      const duckdb = await import(DUCKDB_ESM_URL);
      const bundles = duckdb.getJsDelivrBundles();
      const bundle = await duckdb.selectBundle(bundles);

      const workerUrl = URL.createObjectURL(
        new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" })
      );
      const worker = new Worker(workerUrl);
      const logger = new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING);
      const db = new duckdb.AsyncDuckDB(logger, worker);
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      URL.revokeObjectURL(workerUrl);

      const conn = await db.connect();

      if (typeof onStatus === "function") onStatus("Connecting Parquet views (454,051 rows)…");
      const base = getParquetBaseUrl();
      const views = [
        ["preprints", `${base}/preprints.parquet`],
        ["clinical_trials", `${base}/clinical_trials.parquet`],
        ["downstream_clinical", `${base}/downstream_clinical.parquet`],
      ];

      for (const [viewName, fileUrl] of views) {
        const escapedUrl = fileUrl.replace(/'/g, "''");
        await conn.query(`CREATE OR REPLACE VIEW ${viewName} AS SELECT * FROM read_parquet('${escapedUrl}')`);
      }

      dbInstance = db;
      connInstance = conn;
      return conn;
    })();

    try {
      return await initPromise;
    } catch (err) {
      initPromise = null;
      throw err;
    }
  }

  async function runQuery(rawSql, onStatus) {
    const checked = validateSql(rawSql);
    if (!checked.ok) {
      throw new Error(checked.error);
    }
    const conn = await init(onStatus);
    if (typeof onStatus === "function") onStatus("Executing SQL across 454,051 preprints…");

    const t0 = performance.now();
    const arrowTable = await conn.query(checked.sql);
    const elapsedMs = Math.round(performance.now() - t0);

    const columns = arrowTable.schema.fields.map((f) => f.name);
    const totalRows = arrowTable.numRows;
    const rawArr = arrowTable.toArray();
    const rows = rawArr.slice(0, 100).map((rowObj) => {
      const clean = {};
      for (const col of columns) {
        clean[col] = normalizeArrowValue(rowObj[col]);
      }
      return clean;
    });

    return {
      sql: checked.sql,
      columns,
      rows,
      totalRows,
      elapsedMs,
    };
  }

  window.BioMedRxivDuckDB = {
    isReady: () => Boolean(connInstance),
    init,
    runQuery,
    validateSql,
    getParquetBaseUrl,
  };
})();
