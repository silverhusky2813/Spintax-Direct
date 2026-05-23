/**
 * apps_script_v4.gs
 * ==================
 * Stage 7 Apps Script: adds reply detection on top of Stage 5's smart sending.
 *
 * Two upgrades from v3:
 *   1. SEND PATH: switches to draft-then-send so we capture the Gmail thread_id
 *      at send time (audit error 7.9). Stored on the Emails row.
 *   2. NEW FUNCTION: scanReplies() — reads the inbox, matches replies to sent
 *      rows (thread ID primary, subject fallback), classifies each
 *      (genuine/auto/bounce/unsubscribe), and updates the Emails row +
 *      suppression list. Idempotent via reply_log (audit error 7.17).
 *
 * The sending logic from v3 is preserved (priority sort, sender rotation,
 * rate limiting, lock). This file REPLACES v3 — it's a superset.
 *
 * INSTALLATION:
 *   1. Run schema_setup_v5.py first (adds reply columns + reply_log + tracking_meta)
 *   2. Paste this over v3 in the Apps Script editor
 *   3. Run sendQueuedEmails() once to re-authorize (now needs inbox read scope)
 *   4. Run installReplysScanTrigger() to scan replies every 15 min
 *
 * NOTE: scanReplies needs Gmail read permission. The first manual run will
 * prompt for the additional scope.
 */

// ============================================================================
// CONFIG (shared with v3)
// ============================================================================

var EMAILS_TAB = "Emails";
var ACCOUNTS_TAB = "sender_accounts";
var SEND_LOG_TAB = "send_log";
var REPLY_LOG_TAB = "reply_log";
var SUPPRESSION_TAB = "Suppression";
var TRACKING_META_TAB = "tracking_meta";

var MAX_SENDS_PER_RUN = 15;
var MAX_ATTEMPTS = 3;
var SEND_LOG_MAX_ROWS = 10000;

var REPLY_SCAN_MAX_THREADS = 100;   // per scan run

var PERMANENT_ERROR_PATTERNS = [
  "no such user", "does not exist", "address rejected", "user unknown",
  "mailbox unavailable", "invalid recipient", "550 5.1.1",
  "recipient address rejected",
];

// Reply classification patterns (mirror stage7_reply_classifier.py)
var BOUNCE_SENDER_PATTERNS = ["mailer-daemon@", "postmaster@", "mail-daemon@", "bounce@"];
var BOUNCE_CONTENT_PATTERNS = [
  "delivery status notification", "undelivered mail", "delivery has failed",
  "could not be delivered", "delivery failure", "returned mail",
  "mail delivery failed", "address not found", "recipient address rejected", "550 5.1.1",
];
var AUTO_REPLY_PATTERNS = [
  "out of office", "out of the office", "automatic reply", "auto-reply",
  "autoreply", "away from my desk", "on vacation", "on holiday", "annual leave",
  "i am currently away", "currently out", "abwesenheit", "réponse automatique",
  "no longer with", "has left the company",
];
var UNSUBSCRIBE_PATTERNS = [
  "unsubscribe", "remove me", "take me off", "opt out", "opt-out",
  "stop emailing", "stop contacting", "do not contact", "don't contact",
  "no longer wish to receive", "please remove", "remove from your list",
];


// ============================================================================
// SENDING — preserved from v3, with draft-then-send for thread ID capture
// ============================================================================

function sendQueuedEmails() {
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(0)) {
    Logger.log("Another send run in progress. Exiting.");
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
  if (!emailsSheet) { Logger.log("ERROR: Emails tab not found."); return; }

  var data = emailsSheet.getDataRange().getValues();
  if (data.length < 2) { Logger.log("No data rows."); return; }

  var headers = data[0];
  var hmap = buildHeaderMap(headers);
  if (!validateRequiredColumns(hmap)) return;

  var accounts = loadAccounts(ss);
  var usage = loadUsageFromLog(ss);
  var now = new Date();

  var candidates = [];
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var status = String(getCell(row, hmap, "status") || "").toLowerCase();
    if (status !== "queued") continue;

    var nextRetry = getCell(row, hmap, "next_retry_at");
    if (nextRetry && String(nextRetry).length > 0) {
      var rd = parseDate(nextRetry);
      if (rd && rd > now) continue;
    }
    var score = parseFloat(getCell(row, hmap, "priority_score") || "0") || 0;
    candidates.push({ rowNum: i + 1, score: score, row: row });
  }

  candidates.sort(function (a, b) { return b.score - a.score; });

  var sent = 0, failed = 0, deferred = 0;
  var sendLogAppends = [];

  for (var c = 0; c < candidates.length; c++) {
    if (sent >= MAX_SENDS_PER_RUN) break;
    var cand = candidates[c];
    var row = cand.row, rowNum = cand.rowNum;

    var recipient = String(getCell(row, hmap, "recipient_email") || "");
    var attemptCount = parseInt(getCell(row, hmap, "attempt_count") || "0", 10);

    if (attemptCount >= MAX_ATTEMPTS) {
      updateCells(emailsSheet, rowNum, hmap, {
        status: "Bounced", error_message: "Exceeded max attempts", last_attempt_at: nowIso(),
      });
      failed++;
      continue;
    }

    var preferred = String(getCell(row, hmap, "from_account") || "");
    var account = pickAvailableAccount(preferred, recipient, attemptCount, accounts, usage);
    if (!account) {
      var deferUntil = new Date(now.getTime() + 60 * 60 * 1000);
      updateCells(emailsSheet, rowNum, hmap, { next_retry_at: isoFromDate(deferUntil) });
      deferred++;
      continue;
    }

    var result = attemptSend(row, hmap, account.from_account);

    if (result.success) {
      updateCells(emailsSheet, rowNum, hmap, {
        status: "Sent",
        from_account: account.from_account,
        thread_id: result.threadId || "",   // NEW: capture thread ID (audit 7.9)
        sent_at: nowIso(),
        last_attempt_at: nowIso(),
        attempt_count: attemptCount + 1,
        error_message: "",
        next_retry_at: "",
        reply_status: "none",               // initialize for Stage 7
      });
      bumpUsage(usage, account.from_account);
      sendLogAppends.push([
        nowIso(), account.from_account, recipient,
        String(getCell(row, hmap, "campaign_id") || ""),
        String(getCell(row, hmap, "idempotency_key") || ""),
      ]);
      sent++;
    } else {
      if (isPermanentError(result.error)) {
        updateCells(emailsSheet, rowNum, hmap, {
          status: "Bounced", last_attempt_at: nowIso(),
          attempt_count: attemptCount + 1, error_message: "PERMANENT: " + result.error,
        });
      } else {
        var backoff = Math.pow(2, attemptCount) * 5;
        var nextRetry2 = new Date(now.getTime() + backoff * 60 * 1000);
        updateCells(emailsSheet, rowNum, hmap, {
          status: "Queued", last_attempt_at: nowIso(),
          attempt_count: attemptCount + 1, error_message: "TRANSIENT: " + result.error,
          next_retry_at: isoFromDate(nextRetry2),
        });
      }
      failed++;
    }
  }

  if (sendLogAppends.length > 0) appendSendLog(ss, sendLogAppends);
  trimSendLog(ss);
  Logger.log("Send run: sent=" + sent + " failed=" + failed + " deferred=" + deferred);
}


function attemptSend(row, hmap, fromAccount) {
  /**
   * Draft-then-send so we can capture the thread ID (audit error 7.9).
   * GmailApp.sendEmail() returns nothing useful; createDraft().send()
   * returns the GmailMessage, from which we get the thread.
   */
  var recipient = String(getCell(row, hmap, "recipient_email") || "");
  var subject = String(getCell(row, hmap, "subject") || "");
  var plainBody = String(getCell(row, hmap, "body") || "");
  var htmlBody = String(getCell(row, hmap, "html_body") || "");

  if (!recipient) return { success: false, error: "Missing recipient" };
  if (!subject) return { success: false, error: "Missing subject" };
  if (!plainBody) return { success: false, error: "Missing body" };

  var options = {};
  if (htmlBody && htmlBody.trim().length > 0) options.htmlBody = htmlBody;
  if (fromAccount && fromAccount.trim().length > 0) {
    var aliases = GmailApp.getAliases();
    if (aliases.indexOf(fromAccount.trim()) !== -1) options.from = fromAccount.trim();
  }

  try {
    var draft = GmailApp.createDraft(recipient, subject, plainBody, options);
    var msg = draft.send();   // returns GmailMessage
    var threadId = "";
    try {
      threadId = msg.getThread().getId();
    } catch (e2) {
      // If thread retrieval fails, send still succeeded — just no thread ID
      threadId = "";
    }
    return { success: true, threadId: threadId };
  } catch (e) {
    return { success: false, error: String(e.message || e).substring(0, 500) };
  }
}


// ============================================================================
// REPLY SCANNING — NEW in v4 (audit errors 7.3, 7.4, 7.10, 7.13, 7.17)
// ============================================================================

function scanReplies() {
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(0)) {
    Logger.log("Another scan in progress. Exiting.");
    return;
  }
  try {
    _runReplyScan();
  } finally {
    lock.releaseLock();
  }
}


function _runReplyScan() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var emailsSheet = ss.getSheetByName(EMAILS_TAB);
  if (!emailsSheet) { Logger.log("ERROR: Emails tab missing."); return; }

  var data = emailsSheet.getDataRange().getValues();
  var headers = data[0];
  var hmap = buildHeaderMap(headers);

  // Build lookup structures from sent rows
  var sentRows = [];          // for subject fallback matching
  var byThreadId = {};        // thread_id → array of {rowNum, sentAt}
  var byKey = {};             // idempotency_key → rowNum (for updates)

  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var status = String(getCell(row, hmap, "status") || "").toLowerCase();
    if (status !== "sent" && status !== "delivered") continue;

    var tid = String(getCell(row, hmap, "thread_id") || "").trim();
    var key = String(getCell(row, hmap, "idempotency_key") || "");
    var rowObj = {
      rowNum: i + 1,
      thread_id: tid,
      recipient_email: String(getCell(row, hmap, "recipient_email") || ""),
      subject: String(getCell(row, hmap, "subject") || ""),
      sent_at: getCell(row, hmap, "sent_at"),
      idempotency_key: key,
    };
    sentRows.push(rowObj);
    if (tid) {
      if (!byThreadId[tid]) byThreadId[tid] = [];
      byThreadId[tid].push(rowObj);
    }
    if (key) byKey[key] = i + 1;
  }

  // Load already-processed reply message IDs (idempotency — audit 7.17)
  var processedIds = loadProcessedReplyIds(ss);

  // Watermark: only scan threads newer than last scan (audit 7.10)
  var lastScan = getTrackingMeta(ss, "last_reply_scan_at");
  var searchQuery = "in:inbox";
  if (lastScan) {
    // Gmail search uses YYYY/MM/DD; subtract a day for safety overlap
    var lastDate = parseDate(lastScan);
    if (lastDate) {
      var d = new Date(lastDate.getTime() - 24 * 60 * 60 * 1000);
      searchQuery += " after:" + Utilities.formatDate(d, "UTC", "yyyy/MM/dd");
    }
  }

  var threads = GmailApp.search(searchQuery, 0, REPLY_SCAN_MAX_THREADS);
  var replyLogAppends = [];
  var suppressionAppends = [];
  var matched = 0, unmatched = 0;

  for (var t = 0; t < threads.length; t++) {
    var thread = threads[t];
    var threadId = thread.getId();
    var messages = thread.getMessages();

    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      var msgId = msg.getId();

      // Skip already-processed (idempotency)
      if (processedIds[msgId]) continue;

      // Skip messages WE sent (we only care about inbound)
      var fromRaw = msg.getFrom();
      if (isFromUs(fromRaw)) continue;

      var fromEmail = extractEmail(fromRaw);
      var subject = msg.getSubject() || "";
      var snippet = (msg.getPlainBody() || "").substring(0, 200);
      var receivedAt = msg.getDate();

      // Match to a sent row (thread ID primary, subject fallback)
      var matchRow = matchReply(threadId, fromEmail, subject, byThreadId, sentRows);

      if (!matchRow) {
        unmatched++;
        // Still mark processed so we don't re-examine it forever
        processedIds[msgId] = true;
        continue;
      }

      // Classify
      var replyStatus = classifyReply(fromEmail, subject, snippet);

      // Update the Emails row
      updateCells(emailsSheet, matchRow.rowNum, hmap, {
        reply_status: replyStatus,
        replied_at: isoFromDate(receivedAt),
        reply_snippet: snippet.replace(/\n/g, " ").substring(0, 200),
      });

      // Suppress if unsubscribe or bounce (audit: respect opt-outs)
      if (replyStatus === "unsubscribe" || replyStatus === "bounce") {
        suppressionAppends.push([
          matchRow.recipient_email.toLowerCase(),
          nowIso(),
          replyStatus,
          "",  // campaign_id optional here
        ]);
      }

      // Log the processed reply (idempotency record)
      replyLogAppends.push([
        msgId, threadId, matchRow.idempotency_key, replyStatus,
        fromEmail, subject, isoFromDate(receivedAt), nowIso(),
      ]);
      processedIds[msgId] = true;
      matched++;
    }
  }

  if (replyLogAppends.length > 0) appendRows(ss, REPLY_LOG_TAB, replyLogAppends);
  if (suppressionAppends.length > 0) appendRows(ss, SUPPRESSION_TAB, suppressionAppends);

  // Update watermark
  setTrackingMeta(ss, "last_reply_scan_at", nowIso());

  Logger.log("Reply scan: matched=" + matched + " unmatched=" + unmatched);
}


// ============================================================================
// REPLY MATCHING (mirrors stage7_subject_matcher.py)
// ============================================================================

function matchReply(threadId, fromEmail, subject, byThreadId, sentRows) {
  // Primary: thread ID
  if (threadId && byThreadId[threadId]) {
    return mostRecentlySent(byThreadId[threadId]);
  }
  // Fallback: subject + recipient
  var normSubject = normalizeSubject(subject);
  var normSender = String(fromEmail || "").trim().toLowerCase();
  var candidates = [];
  for (var i = 0; i < sentRows.length; i++) {
    var r = sentRows[i];
    if (String(r.recipient_email).trim().toLowerCase() !== normSender) continue;
    if (normalizeSubject(r.subject) !== normSubject) continue;
    candidates.push(r);
  }
  if (candidates.length === 0) return null;
  return mostRecentlySent(candidates);
}


function mostRecentlySent(rows) {
  var best = null, bestTime = -1;
  for (var i = 0; i < rows.length; i++) {
    var d = parseDate(rows[i].sent_at);
    var t = d ? d.getTime() : 0;
    if (t > bestTime) { bestTime = t; best = rows[i]; }
  }
  return best;
}


function normalizeSubject(subject) {
  if (!subject) return "";
  var s = String(subject);
  var prefixRe = /^(\s*(re|fwd|fw|aw|wg|rv|sv|antw)\s*:\s*)+/i;
  var prev = null;
  while (prev !== s) { prev = s; s = s.replace(prefixRe, ""); }
  return s.replace(/\s+/g, " ").trim().toLowerCase();
}


// ============================================================================
// REPLY CLASSIFICATION (mirrors stage7_reply_classifier.py)
// ============================================================================

function classifyReply(fromEmail, subject, bodySnippet) {
  var combined = ((subject || "") + " " + (bodySnippet || "")).toLowerCase();
  var sender = String(fromEmail || "").toLowerCase();

  // 1. Bounce
  if (anyMatch(sender, BOUNCE_SENDER_PATTERNS)) return "bounce";
  if (anyMatch(combined, BOUNCE_CONTENT_PATTERNS)) return "bounce";
  // 2. Unsubscribe
  if (anyMatch(combined, UNSUBSCRIBE_PATTERNS)) return "unsubscribe";
  // 3. Auto-reply
  if (anyMatch(combined, AUTO_REPLY_PATTERNS)) return "auto_reply";
  // 4. Genuine
  return "genuine";
}


function anyMatch(text, patterns) {
  for (var i = 0; i < patterns.length; i++) {
    if (text.indexOf(patterns[i]) !== -1) return true;
  }
  return false;
}


// ============================================================================
// GMAIL HELPERS
// ============================================================================

function isFromUs(fromRaw) {
  /** True if the message was sent by one of our own accounts/aliases. */
  var addr = extractEmail(fromRaw).toLowerCase();
  var me = Session.getActiveUser().getEmail().toLowerCase();
  if (addr === me) return true;
  var aliases = GmailApp.getAliases();
  for (var i = 0; i < aliases.length; i++) {
    if (addr === aliases[i].toLowerCase()) return true;
  }
  return false;
}


function extractEmail(fromRaw) {
  /** Pull the bare email from "Name <email@x.com>" format. */
  if (!fromRaw) return "";
  var m = String(fromRaw).match(/<([^>]+)>/);
  if (m) return m[1].trim();
  return String(fromRaw).trim();
}


// ============================================================================
// REPLY LOG / IDEMPOTENCY (audit error 7.17)
// ============================================================================

function loadProcessedReplyIds(ss) {
  var sheet = ss.getSheetByName(REPLY_LOG_TAB);
  var processed = {};
  if (!sheet) return processed;
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return processed;
  var hmap = buildHeaderMap(data[0]);
  for (var i = 1; i < data.length; i++) {
    var id = String(getCell(data[i], hmap, "reply_message_id") || "");
    if (id) processed[id] = true;
  }
  return processed;
}


// ============================================================================
// TRACKING META (watermark — audit error 7.10)
// ============================================================================

function getTrackingMeta(ss, key) {
  var sheet = ss.getSheetByName(TRACKING_META_TAB);
  if (!sheet) return null;
  var data = sheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);
  for (var i = 1; i < data.length; i++) {
    if (String(getCell(data[i], hmap, "key")) === key) {
      return getCell(data[i], hmap, "value");
    }
  }
  return null;
}


function setTrackingMeta(ss, key, value) {
  var sheet = ss.getSheetByName(TRACKING_META_TAB);
  if (!sheet) return;
  var data = sheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);
  for (var i = 1; i < data.length; i++) {
    if (String(getCell(data[i], hmap, "key")) === key) {
      updateCells(sheet, i + 1, hmap, { value: value, updated_at: nowIso() });
      return;
    }
  }
  // Not found — append
  sheet.appendRow([key, value, nowIso()]);
}


// ============================================================================
// SHARED HELPERS (from v3)
// ============================================================================

function loadAccounts(ss) {
  var sheet = ss.getSheetByName(ACCOUNTS_TAB);
  if (!sheet) return [defaultAccount()];
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
      // Stage 6: warm-up fields
      warmup_enabled: String(getCell(r, hmap, "warmup_enabled") || "FALSE").toUpperCase() === "TRUE",
      activated_at: getCell(r, hmap, "activated_at") || "",
    });
  }
  return accounts.length ? accounts : [defaultAccount()];
}

function defaultAccount() {
  return {
    from_account: "daniel@premiumads.net", daily_cap: 200, hourly_cap: 30,
    window_start: 0, window_end: 24, is_active: true, priority_order: 0,
    warmup_enabled: false, activated_at: "",
  };
}

// ----------------------------------------------------------------------------
// Stage 6: warm-up cap logic (mirrors stage6_warmup.py)
// ----------------------------------------------------------------------------

var WARMUP_SCHEDULE = [
  [1, 20], [3, 40], [5, 60], [8, 100], [12, 150], [16, 200], [22, 300], [29, null]
];

function daysActive(activatedAt, now) {
  if (!activatedAt) return 0;
  var dt = parseDate(activatedAt);
  if (!dt) return 0;
  var actDate = new Date(Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate()));
  var nowDate = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  var delta = Math.floor((nowDate - actDate) / (24 * 3600 * 1000));
  return Math.max(0, delta + 1);
}

function warmupCapForDay(day) {
  if (day <= 0) return WARMUP_SCHEDULE[0][1];
  var cap = WARMUP_SCHEDULE[0][1];
  for (var i = 0; i < WARMUP_SCHEDULE.length; i++) {
    if (day >= WARMUP_SCHEDULE[i][0]) cap = WARMUP_SCHEDULE[i][1];
    else break;
  }
  return cap;  // may be null = graduated
}

function effectiveDailyCap(account, now) {
  if (!account.warmup_enabled) return account.daily_cap;
  var day = daysActive(account.activated_at, now);
  var wcap = warmupCapForDay(day);
  if (wcap === null) return account.daily_cap;
  return Math.min(account.daily_cap, wcap);  // only lowers (audit 6.3)
}

function loadUsageFromLog(ss) {
  var usage = {};
  var sheet = ss.getSheetByName(SEND_LOG_TAB);
  if (!sheet) return usage;
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return usage;
  var hmap = buildHeaderMap(data[0]);
  var now = new Date();
  var c24 = new Date(now.getTime() - 24 * 3600 * 1000);
  var c1 = new Date(now.getTime() - 3600 * 1000);
  for (var i = 1; i < data.length; i++) {
    var acct = String(getCell(data[i], hmap, "from_account") || "").trim().toLowerCase();
    if (!acct) continue;
    var sentAt = parseDate(getCell(data[i], hmap, "sent_at"));
    if (!sentAt) continue;
    if (!usage[acct]) usage[acct] = { day: 0, hour: 0 };
    if (sentAt >= c24) usage[acct].day++;
    if (sentAt >= c1) usage[acct].hour++;
  }
  return usage;
}

function bumpUsage(usage, account) {
  var k = account.trim().toLowerCase();
  if (!usage[k]) usage[k] = { day: 0, hour: 0 };
  usage[k].day++; usage[k].hour++;
}

function accountIsAvailable(account, usage, now) {
  if (!account.is_active) return false;
  if (!(account.window_start === 0 && account.window_end === 24)) {
    var hour = now.getUTCHours();
    var inWin = account.window_start <= account.window_end
      ? (hour >= account.window_start && hour < account.window_end)
      : (hour >= account.window_start || hour < account.window_end);
    if (!inWin) return false;
  }
  var k = account.from_account.trim().toLowerCase();
  var u = usage[k] || { day: 0, hour: 0 };
  // Stage 6: effective cap applies warm-up restriction (audit 6.3)
  var effCap = effectiveDailyCap(account, now);
  if (u.day >= effCap) return false;
  if (u.hour >= account.hourly_cap) return false;
  return true;
}

function pickAvailableAccount(preferred, recipient, attemptCount, accounts, usage) {
  var now = new Date();
  if (preferred) {
    for (var i = 0; i < accounts.length; i++) {
      if (accounts[i].from_account.toLowerCase() === preferred.toLowerCase()) {
        if (accountIsAvailable(accounts[i], usage, now)) return accounts[i];
        break;
      }
    }
  }
  var active = accounts.filter(function (a) { return a.is_active; });
  active.sort(function (a, b) { return a.priority_order - b.priority_order; });
  if (!active.length) return null;
  var pIdx = hashToIndex(recipient, active.length, 0);
  if (accountIsAvailable(active[pIdx], usage, now)) return active[pIdx];
  var avail = active.filter(function (a) { return accountIsAvailable(a, usage, now); });
  if (!avail.length) return null;
  return avail[hashToIndex(recipient, avail.length, attemptCount)];
}

function hashToIndex(email, n, offset) {
  if (n <= 0) return 0;
  var s = String(email || "").trim().toLowerCase();
  var hash = 5381;
  for (var i = 0; i < s.length; i++) hash = ((hash << 5) + hash + s.charCodeAt(i)) & 0x7fffffff;
  return (hash + offset) % n;
}

function isPermanentError(msg) {
  var lower = String(msg || "").toLowerCase();
  for (var i = 0; i < PERMANENT_ERROR_PATTERNS.length; i++) {
    if (lower.indexOf(PERMANENT_ERROR_PATTERNS[i]) !== -1) return true;
  }
  return false;
}

function appendSendLog(ss, rows) { appendRows(ss, SEND_LOG_TAB, rows); }

function appendRows(ss, tabName, rows) {
  var sheet = ss.getSheetByName(tabName);
  if (!sheet) return;
  var lastRow = sheet.getLastRow();
  sheet.getRange(lastRow + 1, 1, rows.length, rows[0].length).setValues(rows);
}

function trimSendLog(ss) {
  var sheet = ss.getSheetByName(SEND_LOG_TAB);
  if (!sheet) return;
  var n = sheet.getLastRow();
  if (n - 1 > SEND_LOG_MAX_ROWS) {
    sheet.deleteRows(2, (n - 1) - SEND_LOG_MAX_ROWS);
  }
}

function buildHeaderMap(headers) {
  var map = {};
  for (var i = 0; i < headers.length; i++) {
    var h = String(headers[i] || "").trim();
    if (h) map[h] = i;
  }
  return map;
}

function validateRequiredColumns(hmap) {
  var req = ["status", "recipient_email", "subject", "body"];
  var missing = [];
  for (var i = 0; i < req.length; i++) {
    if (typeof hmap[req[i]] === "undefined") missing.push(req[i]);
  }
  if (missing.length) { Logger.log("ERROR: Missing columns: " + missing.join(", ")); return false; }
  return true;
}

function getCell(row, hmap, name) {
  var idx = hmap[name];
  if (typeof idx === "undefined") return undefined;
  return row[idx];
}

function updateCells(sheet, rowNum, hmap, updates) {
  for (var col in updates) {
    var idx = hmap[col];
    if (typeof idx === "undefined") continue;
    sheet.getRange(rowNum, idx + 1).setValue(updates[col]);
  }
}

function nowIso() { return Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'"); }
function isoFromDate(d) { return Utilities.formatDate(d, "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'"); }
function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return value;
  var s = String(value).replace(/\s*\([^)]*\)\s*$/, "");
  var d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}


// ============================================================================
// TRIGGERS
// ============================================================================

function installFiveMinuteTrigger() {
  removeTrigger("sendQueuedEmails");
  ScriptApp.newTrigger("sendQueuedEmails").timeBased().everyMinutes(5).create();
  Logger.log("Installed 5-min send trigger.");
}

function installReplyScanTrigger() {
  removeTrigger("scanReplies");
  ScriptApp.newTrigger("scanReplies").timeBased().everyMinutes(15).create();
  Logger.log("Installed 15-min reply scan trigger.");
}

// ----------------------------------------------------------------------------
// Stage 6: scheduled account health check + auto-pause
// ----------------------------------------------------------------------------

var BOUNCE_RATE_WARNING = 3.0;
var BOUNCE_RATE_CRITICAL = 5.0;
var MIN_SENDS_FOR_HEALTH = 20;
var HEALTH_WINDOW_DAYS = 7;
var REACTIVATION_GRACE_HOURS = 24;

function checkAccountHealth() {
  /**
   * Scheduled health check (mirrors stage6_health_score.py + enforcement).
   * Auto-pauses accounts whose 7d bounce rate exceeds critical, with the
   * last-account and reactivation-grace guards.
   */
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(0)) { Logger.log("Health check: another run in progress."); return; }
  try {
    _runHealthCheck();
  } finally {
    lock.releaseLock();
  }
}

function _runHealthCheck() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var accountsSheet = ss.getSheetByName(ACCOUNTS_TAB);
  if (!accountsSheet) { Logger.log("Health check: no accounts tab."); return; }

  var accounts = loadAccounts(ss);
  var now = new Date();
  var cutoff = new Date(now.getTime() - HEALTH_WINDOW_DAYS * 24 * 3600 * 1000);

  // Count active accounts (audit 6.1)
  var activeCount = 0;
  for (var a = 0; a < accounts.length; a++) {
    if (accounts[a].is_active) activeCount++;
  }

  // Count sends/bounces per account from Emails
  var emailsSheet = ss.getSheetByName(EMAILS_TAB);
  var data = emailsSheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);

  var stats = {};  // account → {sends, bounces}
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var acct = String(getCell(row, hmap, "from_account") || "").trim().toLowerCase();
    if (!acct) continue;
    var status = String(getCell(row, hmap, "status") || "").toLowerCase();
    var replyStatus = String(getCell(row, hmap, "reply_status") || "").toLowerCase();
    var ts = parseDate(getCell(row, hmap, "sent_at")) || parseDate(getCell(row, hmap, "last_attempt_at"));
    if (!ts || ts < cutoff) continue;

    if (!stats[acct]) stats[acct] = { sends: 0, bounces: 0 };
    if (status === "sent" || status === "delivered" || status === "bounced") stats[acct].sends++;
    if (status === "bounced" || replyStatus === "bounce") stats[acct].bounces++;
  }

  // Assess + enforce
  var aHeaders = accountsSheet.getDataRange().getValues()[0];
  var ahmap = buildHeaderMap(aHeaders);
  var logRows = [];

  for (var j = 0; j < accounts.length; j++) {
    var account = accounts[j];
    var key = account.from_account.trim().toLowerCase();
    var s = stats[key] || { sends: 0, bounces: 0 };
    var rate = s.sends > 0 ? (100.0 * s.bounces / s.sends) : 0.0;
    rate = Math.round(rate * 10) / 10;

    var healthStatus = "healthy";
    var actionTaken = "none";

    if (s.sends < MIN_SENDS_FOR_HEALTH) {
      healthStatus = "insufficient_data";
    } else if (rate < BOUNCE_RATE_WARNING) {
      healthStatus = "healthy";
    } else if (rate < BOUNCE_RATE_CRITICAL) {
      healthStatus = "warning";
      actionTaken = "alerted";
      Logger.log("ALERT: " + account.from_account + " bounce rate " + rate + "%");
    } else {
      healthStatus = "critical";
      // Grace window guard (audit 6.7)
      var reactivatedAt = getAccountField(accountsSheet, ahmap, account.from_account, "reactivated_at");
      var inGrace = false;
      if (reactivatedAt) {
        var rDt = parseDate(reactivatedAt);
        if (rDt && (now - rDt) < REACTIVATION_GRACE_HOURS * 3600 * 1000) inGrace = true;
      }

      if (inGrace) {
        actionTaken = "in_grace";
        Logger.log(account.from_account + " critical but in grace — not pausing");
      } else if (activeCount <= 1) {
        // Last-account guard (audit 6.1)
        actionTaken = "alerted";
        Logger.log("CRITICAL: " + account.from_account + " bounce " + rate +
                   "% but it's the LAST active account — NOT pausing. Manual fix needed.");
      } else {
        // Safe to pause
        pauseAccountRow(accountsSheet, ahmap, account.from_account,
          "Auto-paused: bounce rate " + rate + "% (" + s.bounces + "/" + s.sends + ") over " + HEALTH_WINDOW_DAYS + "d");
        actionTaken = "auto_paused";
        activeCount--;  // reflect the pause for subsequent accounts this run
        Logger.log("AUTO-PAUSED: " + account.from_account + " bounce " + rate + "%");
      }
    }

    logRows.push([nowIso(), account.from_account, s.sends, s.bounces, rate, healthStatus, actionTaken]);
  }

  // Log snapshots
  if (logRows.length > 0) {
    var logSheet = ss.getSheetByName("account_health_log");
    if (logSheet) {
      var lastRow = logSheet.getLastRow();
      logSheet.getRange(lastRow + 1, 1, logRows.length, logRows[0].length).setValues(logRows);
    }
  }

  Logger.log("Health check complete. Assessed " + accounts.length + " account(s).");
}

function getAccountField(sheet, hmap, fromAccount, field) {
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (String(getCell(data[i], hmap, "from_account") || "").trim().toLowerCase() ===
        fromAccount.trim().toLowerCase()) {
      return getCell(data[i], hmap, field);
    }
  }
  return null;
}

function pauseAccountRow(sheet, hmap, fromAccount, reason) {
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (String(getCell(data[i], hmap, "from_account") || "").trim().toLowerCase() ===
        fromAccount.trim().toLowerCase()) {
      var rowNum = i + 1;
      updateCells(sheet, rowNum, hmap, {
        is_active: "FALSE",
        paused_reason: reason,
        paused_at: nowIso(),
      });
      return;
    }
  }
}

function installHealthCheckTrigger() {
  removeTrigger("checkAccountHealth");
  // Run health check every 6 hours
  ScriptApp.newTrigger("checkAccountHealth").timeBased().everyHours(6).create();
  Logger.log("Installed 6-hour health check trigger.");
}

function removeTrigger(fnName) {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === fnName) ScriptApp.deleteTrigger(triggers[i]);
  }
}

function resetFailedRowsToQueued() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(EMAILS_TAB);
  var data = sheet.getDataRange().getValues();
  var hmap = buildHeaderMap(data[0]);
  var count = 0;
  for (var i = 1; i < data.length; i++) {
    var s = String(getCell(data[i], hmap, "status") || "").toLowerCase();
    if (s === "failed" || s === "bounced") {
      updateCells(sheet, i + 1, hmap, { status: "Queued", attempt_count: 0, error_message: "", next_retry_at: "" });
      count++;
    }
  }
  Logger.log("Reset " + count + " rows.");
}
