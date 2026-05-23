/**
 * apps_script_v3.gs
 * ==================
 * Stage 5 Apps Script: priority-ordered, rate-limited, multi-account sending.
 *
 * Upgrades from v2:
 *   - Sorts queue by priority_score DESC before sending (audit error 5.1, 5.16)
 *   - Re-validates sender account caps at send time (audit error 5.14)
 *   - Reassigns sender via round-robin if preferred account exhausted
 *   - Logs each send to send_log tab for rolling rate-limit tracking
 *   - Respects next_retry_at (skips rows scheduled for the future)
 *   - Document lock prevents overlapping runs (audit error 5.8)
 *   - send_log auto-trim keeps tab from growing unbounded (audit error 5.9)
 *
 * READS from tabs: Emails, sender_accounts, send_log
 * WRITES to tabs:  Emails (status updates), send_log (append)
 *
 * INSTALLATION:
 *   1. Run schema_setup_v4.py first (creates sender_accounts + send_log tabs)
 *   2. Extensions → Apps Script → paste this (replaces v2)
 *   3. Run sendQueuedEmails() once manually to authorize
 *   4. Run installFiveMinuteTrigger() for automation
 */

// ============================================================================
// CONFIG
// ============================================================================

var EMAILS_TAB = "Emails";
var ACCOUNTS_TAB = "sender_accounts";
var SEND_LOG_TAB = "send_log";

var MAX_SENDS_PER_RUN = 15;       // global per-run safety cap
var MAX_ATTEMPTS = 3;             // give up after N tries
var SEND_LOG_MAX_ROWS = 10000;    // trim send_log beyond this (audit 5.9)

// Known-permanent failure substrings (audit error 5.4).
// Anything matching → mark Bounced immediately, no retry.
var PERMANENT_ERROR_PATTERNS = [
  "no such user",
  "does not exist",
  "address rejected",
  "user unknown",
  "mailbox unavailable",
  "invalid recipient",
  "550 5.1.1",
  "recipient address rejected",
];


// ============================================================================
// MAIN ENTRY (preserved name for trigger compatibility)
// ============================================================================

function sendQueuedEmails() {
  // Document lock prevents overlapping runs (audit error 5.8)
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(0)) {
    Logger.log("Another run is in progress. Exiting.");
    return;
  }

  try {
    _runSendCycle();
  } finally {
    lock.releaseLock();
  }
}


function _runSendCycle() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var emailsSheet = ss.getSheetByName(EMAILS_TAB);
  if (!emailsSheet) {
    Logger.log("ERROR: Emails tab not found.");
    return;
  }

  var data = emailsSheet.getDataRange().getValues();
  if (data.length < 2) {
    Logger.log("No data rows.");
    return;
  }

  var headers = data[0];
  var hmap = buildHeaderMap(headers);
  if (!validateRequiredColumns(hmap)) return;

  // Load account config + current usage
  var accounts = loadAccounts(ss);
  var usage = loadUsageFromLog(ss);  // { account: {day: n, hour: n} }

  var now = new Date();

  // Build a list of sendable row indices with their priority scores
  var candidates = [];
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var status = String(getCell(row, hmap, "status") || "").toLowerCase();
    if (status !== "queued") continue;

    // Respect next_retry_at (audit error 5.5)
    var nextRetry = getCell(row, hmap, "next_retry_at");
    if (nextRetry && String(nextRetry).length > 0) {
      var retryDate = parseDate(nextRetry);
      if (retryDate && retryDate > now) {
        continue; // scheduled for the future — skip this run
      }
    }

    var score = parseFloat(getCell(row, hmap, "priority_score") || "0") || 0;
    candidates.push({ rowIndex: i, rowNum: i + 1, score: score, row: row });
  }

  // Sort by priority score DESC (audit error 5.16)
  candidates.sort(function (a, b) { return b.score - a.score; });

  var sentThisRun = 0;
  var deferredThisRun = 0;
  var failedThisRun = 0;
  var sendLogAppends = [];

  for (var c = 0; c < candidates.length; c++) {
    if (sentThisRun >= MAX_SENDS_PER_RUN) {
      Logger.log("Hit per-run cap (" + MAX_SENDS_PER_RUN + ").");
      break;
    }

    var cand = candidates[c];
    var row = cand.row;
    var rowNum = cand.rowNum;

    var recipient = String(getCell(row, hmap, "recipient_email") || "");
    var attemptCount = parseInt(getCell(row, hmap, "attempt_count") || "0", 10);

    // Max attempts → Bounced
    if (attemptCount >= MAX_ATTEMPTS) {
      updateCells(emailsSheet, rowNum, hmap, {
        status: "Bounced",
        error_message: "Exceeded max " + MAX_ATTEMPTS + " attempts",
        last_attempt_at: nowIso(),
      });
      failedThisRun++;
      continue;
    }

    // ---- Re-validate sender at send time (audit error 5.14) ----
    var preferred = String(getCell(row, hmap, "from_account") || "");
    var chosenAccount = pickAvailableAccount(preferred, recipient, attemptCount, accounts, usage);

    if (!chosenAccount) {
      // All accounts exhausted — defer to next hour (audit error 5.12)
      var deferUntil = new Date(now.getTime() + 60 * 60 * 1000); // +1h
      updateCells(emailsSheet, rowNum, hmap, {
        next_retry_at: isoFromDate(deferUntil),
      });
      deferredThisRun++;
      continue;
    }

    // ---- Attempt send ----
    var sendResult = attemptSend(row, hmap, chosenAccount.from_account);

    if (sendResult.success) {
      updateCells(emailsSheet, rowNum, hmap, {
        status: "Sent",
        from_account: chosenAccount.from_account,  // reflect actual sender
        sent_at: nowIso(),
        last_attempt_at: nowIso(),
        attempt_count: attemptCount + 1,
        error_message: "",
        next_retry_at: "",
      });

      // Track usage in-memory so subsequent picks this run see it
      bumpUsage(usage, chosenAccount.from_account);

      // Queue a send_log append
      sendLogAppends.push([
        nowIso(),
        chosenAccount.from_account,
        recipient,
        String(getCell(row, hmap, "campaign_id") || ""),
        String(getCell(row, hmap, "idempotency_key") || ""),
      ]);

      sentThisRun++;
      Logger.log("✓ Sent row " + rowNum + " via " + chosenAccount.from_account);
    } else {
      // Classify the failure (audit error 5.4)
      var isPermanent = isPermanentError(sendResult.error);
      if (isPermanent) {
        updateCells(emailsSheet, rowNum, hmap, {
          status: "Bounced",
          last_attempt_at: nowIso(),
          attempt_count: attemptCount + 1,
          error_message: "PERMANENT: " + sendResult.error,
        });
      } else {
        // Transient → exponential backoff (audit error 5.5)
        var backoffMin = Math.pow(2, attemptCount) * 5; // 5, 10, 20 min
        var nextRetryDate = new Date(now.getTime() + backoffMin * 60 * 1000);
        updateCells(emailsSheet, rowNum, hmap, {
          status: "Queued",  // stays queued for retry
          last_attempt_at: nowIso(),
          attempt_count: attemptCount + 1,
          error_message: "TRANSIENT: " + sendResult.error,
          next_retry_at: isoFromDate(nextRetryDate),
        });
      }
      failedThisRun++;
      Logger.log("✗ Failed row " + rowNum + ": " + sendResult.error);
    }
  }

  // Batch-append send_log entries (audit error 5.15: status flipped before this)
  if (sendLogAppends.length > 0) {
    appendSendLog(ss, sendLogAppends);
  }

  // Trim send_log if it's grown too large (audit error 5.9)
  trimSendLog(ss);

  Logger.log(
    "Run complete. Sent: " + sentThisRun +
    ", Failed: " + failedThisRun +
    ", Deferred: " + deferredThisRun
  );
}


// ============================================================================
// SENDER SELECTION (mirrors stage5_sender_pool.py — audit error 5.14)
// ============================================================================

function loadAccounts(ss) {
  /** Load sender_accounts into an array of objects. */
  var sheet = ss.getSheetByName(ACCOUNTS_TAB);
  if (!sheet) {
    // Fallback: single default account (audit error 5.17)
    return [{
      from_account: "daniel@premiumads.net",
      daily_cap: 200, hourly_cap: 30,
      window_start: 0, window_end: 24,
      is_active: true, priority_order: 0,
    }];
  }

  var data = sheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);
  var accounts = [];
  for (var i = 1; i < data.length; i++) {
    var r = data[i];
    var email = String(getCell(r, hmap, "from_account") || "").trim();
    if (!email) continue;
    accounts.push({
      from_account: email,
      daily_cap: parseInt(getCell(r, hmap, "daily_cap") || "200", 10),
      hourly_cap: parseInt(getCell(r, hmap, "hourly_cap") || "30", 10),
      window_start: parseInt(getCell(r, hmap, "send_window_start_utc") || "0", 10),
      window_end: parseInt(getCell(r, hmap, "send_window_end_utc") || "24", 10),
      is_active: String(getCell(r, hmap, "is_active") || "TRUE").toUpperCase() === "TRUE",
      priority_order: parseInt(getCell(r, hmap, "priority_order") || "0", 10),
    });
  }

  if (accounts.length === 0) {
    return [{
      from_account: "daniel@premiumads.net",
      daily_cap: 200, hourly_cap: 30,
      window_start: 0, window_end: 24,
      is_active: true, priority_order: 0,
    }];
  }
  return accounts;
}


function loadUsageFromLog(ss) {
  /**
   * Count sends per account in the rolling 24h / 1h windows.
   * Returns { account: { day: n, hour: n } }.
   */
  var usage = {};
  var sheet = ss.getSheetByName(SEND_LOG_TAB);
  if (!sheet) return usage;

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return usage;

  var hmap = buildHeaderMap(data[0]);
  var now = new Date();
  var cutoff24 = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  var cutoff1 = new Date(now.getTime() - 60 * 60 * 1000);

  for (var i = 1; i < data.length; i++) {
    var acct = String(getCell(data[i], hmap, "from_account") || "").trim().toLowerCase();
    if (!acct) continue;
    var sentAt = parseDate(getCell(data[i], hmap, "sent_at"));
    if (!sentAt) continue;

    if (!usage[acct]) usage[acct] = { day: 0, hour: 0 };
    if (sentAt >= cutoff24) usage[acct].day++;
    if (sentAt >= cutoff1) usage[acct].hour++;
  }
  return usage;
}


function bumpUsage(usage, account) {
  var key = account.trim().toLowerCase();
  if (!usage[key]) usage[key] = { day: 0, hour: 0 };
  usage[key].day++;
  usage[key].hour++;
}


function accountIsAvailable(account, usage, now) {
  if (!account.is_active) return false;

  // Window check
  if (!(account.window_start === 0 && account.window_end === 24)) {
    var hour = now.getUTCHours();
    var inWindow;
    if (account.window_start <= account.window_end) {
      inWindow = (hour >= account.window_start && hour < account.window_end);
    } else {
      inWindow = (hour >= account.window_start || hour < account.window_end);
    }
    if (!inWindow) return false;
  }

  // Cap check
  var key = account.from_account.trim().toLowerCase();
  var u = usage[key] || { day: 0, hour: 0 };
  if (u.day >= account.daily_cap) return false;
  if (u.hour >= account.hourly_cap) return false;

  return true;
}


function pickAvailableAccount(preferred, recipient, attemptCount, accounts, usage) {
  /**
   * Hybrid selection mirroring stage5_sender_pool.py.
   * 1. If preferred account is available → use it
   * 2. Else hash-based primary among active
   * 3. Else round-robin among available (offset by attemptCount)
   * 4. Else null
   */
  var now = new Date();

  // Step 1: honor the preferred (assign-time) account if still available
  if (preferred) {
    for (var i = 0; i < accounts.length; i++) {
      if (accounts[i].from_account.toLowerCase() === preferred.toLowerCase()) {
        if (accountIsAvailable(accounts[i], usage, now)) {
          return accounts[i];
        }
        break;
      }
    }
  }

  // Active accounts sorted by priority_order
  var active = accounts.filter(function (a) { return a.is_active; });
  active.sort(function (a, b) { return a.priority_order - b.priority_order; });
  if (active.length === 0) return null;

  // Step 2: hash-based primary
  var primaryIdx = hashToIndex(recipient, active.length, 0);
  if (accountIsAvailable(active[primaryIdx], usage, now)) {
    return active[primaryIdx];
  }

  // Step 3: round-robin among available
  var available = active.filter(function (a) { return accountIsAvailable(a, usage, now); });
  if (available.length === 0) return null;
  var rrIdx = hashToIndex(recipient, available.length, attemptCount);
  return available[rrIdx];
}


function hashToIndex(email, n, offset) {
  /** Deterministic hash → index, mirroring the Python implementation. */
  if (n <= 0) return 0;
  var normalized = String(email || "").trim().toLowerCase();
  // Simple deterministic hash (djb2 variant) — doesn't need to match Python
  // exactly, just needs to be stable + well-distributed within Apps Script.
  var hash = 5381;
  for (var i = 0; i < normalized.length; i++) {
    hash = ((hash << 5) + hash + normalized.charCodeAt(i)) & 0x7fffffff;
  }
  return (hash + offset) % n;
}


// ============================================================================
// SEND
// ============================================================================

function attemptSend(row, hmap, fromAccount) {
  var recipient = String(getCell(row, hmap, "recipient_email") || "");
  var subject = String(getCell(row, hmap, "subject") || "");
  var plainBody = String(getCell(row, hmap, "body") || "");
  var htmlBody = String(getCell(row, hmap, "html_body") || "");

  if (!recipient) return { success: false, error: "Missing recipient" };
  if (!subject) return { success: false, error: "Missing subject" };
  if (!plainBody) return { success: false, error: "Missing body" };

  var options = {};
  if (htmlBody && htmlBody.trim().length > 0) {
    options.htmlBody = htmlBody;
  }

  // Use 'from' only if it's a verified alias of the authenticated user
  if (fromAccount && fromAccount.trim().length > 0) {
    var aliases = GmailApp.getAliases();
    if (aliases.indexOf(fromAccount.trim()) !== -1) {
      options.from = fromAccount.trim();
    }
  }

  try {
    GmailApp.sendEmail(recipient, subject, plainBody, options);
    return { success: true };
  } catch (e) {
    return { success: false, error: String(e.message || e).substring(0, 500) };
  }
}


function isPermanentError(errorMsg) {
  var lower = String(errorMsg || "").toLowerCase();
  for (var i = 0; i < PERMANENT_ERROR_PATTERNS.length; i++) {
    if (lower.indexOf(PERMANENT_ERROR_PATTERNS[i]) !== -1) {
      return true;
    }
  }
  return false;
}


// ============================================================================
// SEND LOG
// ============================================================================

function appendSendLog(ss, rows) {
  var sheet = ss.getSheetByName(SEND_LOG_TAB);
  if (!sheet) return;
  // Append all rows at once
  var lastRow = sheet.getLastRow();
  sheet.getRange(lastRow + 1, 1, rows.length, rows[0].length).setValues(rows);
}


function trimSendLog(ss) {
  /** Keep send_log under SEND_LOG_MAX_ROWS by deleting oldest rows. */
  var sheet = ss.getSheetByName(SEND_LOG_TAB);
  if (!sheet) return;
  var numRows = sheet.getLastRow();
  // -1 for header
  if (numRows - 1 > SEND_LOG_MAX_ROWS) {
    var toDelete = (numRows - 1) - SEND_LOG_MAX_ROWS;
    // Delete from row 2 (just below header) — these are the oldest if appended in order
    sheet.deleteRows(2, toDelete);
    Logger.log("Trimmed " + toDelete + " old send_log rows.");
  }
}


// ============================================================================
// HELPERS
// ============================================================================

function buildHeaderMap(headers) {
  var map = {};
  for (var i = 0; i < headers.length; i++) {
    var h = String(headers[i] || "").trim();
    if (h) map[h] = i;
  }
  return map;
}

function validateRequiredColumns(hmap) {
  var required = ["status", "recipient_email", "subject", "body"];
  var missing = [];
  for (var i = 0; i < required.length; i++) {
    if (typeof hmap[required[i]] === "undefined") missing.push(required[i]);
  }
  if (missing.length > 0) {
    Logger.log("ERROR: Missing columns: " + missing.join(", "));
    return false;
  }
  return true;
}

function getCell(row, hmap, name) {
  var idx = hmap[name];
  if (typeof idx === "undefined") return undefined;
  return row[idx];
}

function updateCells(sheet, rowNum, hmap, updates) {
  for (var colName in updates) {
    var idx = hmap[colName];
    if (typeof idx === "undefined") continue;
    sheet.getRange(rowNum, idx + 1).setValue(updates[colName]);
  }
}

function nowIso() {
  return Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'");
}

function isoFromDate(d) {
  return Utilities.formatDate(d, "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'");
}

function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return value;
  // Try native Date parse; strip parenthetical TZ name if present
  var s = String(value).replace(/\s*\([^)]*\)\s*$/, "");
  var d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}


// ============================================================================
// SETUP HELPERS
// ============================================================================

function installFiveMinuteTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "sendQueuedEmails") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger("sendQueuedEmails").timeBased().everyMinutes(5).create();
  Logger.log("Installed 5-minute trigger.");
}

function resetFailedRowsToQueued() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(EMAILS_TAB);
  var data = sheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);
  var count = 0;
  for (var i = 1; i < data.length; i++) {
    var status = String(getCell(data[i], hmap, "status") || "").toLowerCase();
    if (status === "failed" || status === "bounced") {
      updateCells(sheet, i + 1, hmap, {
        status: "Queued", attempt_count: 0, error_message: "", next_retry_at: "",
      });
      count++;
    }
  }
  Logger.log("Reset " + count + " rows to Queued.");
}
