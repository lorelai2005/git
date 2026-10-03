"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.StreamProtocolError = exports.StreamTimeoutError = exports.HttpResponseError = exports.BACKOFF_MAX_DELAY_MS = exports.DEFAULT_RETRY_DELAY_MS = exports.DEFAULT_MAX_RETRIES = void 0;
exports.isRetryableStatus = isRetryableStatus;
exports.isOverloadedError = isOverloadedError;
exports.isContextWindowError = isContextWindowError;
exports.isConnectionRecoveryError = isConnectionRecoveryError;
exports.isOverloadedWithoutRetryAfter = isOverloadedWithoutRetryAfter;
exports.getRetryAfterMs = getRetryAfterMs;
exports.computeConnectionRecoveryDelayMs = computeConnectionRecoveryDelayMs;
exports.computeRetryDelayMs = computeRetryDelayMs;
exports.isRetryableError = isRetryableError;
exports.sleep = sleep;
exports.interruptibleSleep = interruptibleSleep;
exports.withRetry = withRetry;
exports.streamWithRetry = streamWithRetry;
exports.DEFAULT_MAX_RETRIES = Number(process.env.CRABOT_LLM_MAX_RETRIES) >= 0
    ? Number(process.env.CRABOT_LLM_MAX_RETRIES)
    : 2;
exports.DEFAULT_RETRY_DELAY_MS = 1000;
exports.BACKOFF_MAX_DELAY_MS = 8000;
exports.RETRY_AFTER_MAX_MS = 300000;
exports.OVERLOADED_WITHOUT_RETRY_AFTER_MAX_RETRIES = 3;
const RETRYABLE_CODES = new Set([
    // POSIX
    'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EPIPE',
    'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH', 'ENOTFOUND',
    // undici
    'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT',
    'UND_ERR_BODY_TIMEOUT', 'UND_ERR_SOCKET', 'UND_ERR_CLOSED',
    'UND_ERR_REQ_RETRY',
]);
const RETRYABLE_MESSAGE_PATTERNS = [
    'fetch failed', 'terminated', 'socket hang up', 'network error',
];
// 上游 body 里出现这些 code 时视为过载/限流，走指数退避（包括 HTTP 400 的非标准载体）。
// 目前只见过 ChatGPT Codex 后端用 HTTP 400 + server_is_overloaded；其他 provider 后续按样本补。
const OVERLOADED_BODY_CODES = new Set([
    'server_is_overloaded',
]);
// HTTP status 黑名单：明确指示客户端永久错误（认证 / 越权 / 不存在 / 方法错误 / 校验失败），
// 重试无意义。其他 4xx（含 400）和所有 5xx 都默认走重试 —— 现实中很多上游把 transient
// 错误（过载 / 路由抖动 / token 过期）伪装成 400，按状态码白名单一刀切会错杀整轮请求。
const NON_RETRYABLE_HTTP_STATUS = new Set([401, 403, 404, 405, 422]);
// body code 黑名单：上游把"客户端永久错误"塞进 HTTP 400 body 的特殊 code。
// 这类错误重试也不会成功（同输入再发还是被拦），必须在状态码默认重试之前先短路。
const NON_RETRYABLE_BODY_CODES = new Set([
    'content_filter', // 内容审查命中（OpenAI）
    'data_inspection_failed', // 内容审查命中（阿里云百炼 / DashScope）
    'DataInspectionFailed', // 同上，驼峰变体
    'invalid_prompt', // prompt 结构不合法
    'invalid_request_error', // 通用请求错（OpenAI 风格）
    'invalid_api_key',
    'invalid_authentication',
]);
class HttpResponseError extends Error {
    constructor(status, body, label) {
        super(`${label} HTTP ${status}: ${body.slice(0, 300)}`);
        this.status = status;
        this.body = body;
        this.name = 'HttpResponseError';
    }
    /** body 中的 `code` 字段（如有），用于识别非标准过载/错误码。结果缓存。 */
    get bodyCode() {
        if (this.parsedBodyCode === undefined) {
            this.parsedBodyCode = extractBodyCode(this.body);
        }
        return this.parsedBodyCode;
    }
}
exports.HttpResponseError = HttpResponseError;
/**
 * 流式超时错误：首 chunk 超 TTFB 未到（phase='ttfb'）、或相邻 chunk 间隔超过空闲
 * 阈值（phase='idle'）时由 withStreamTimeout 抛出。视为可重试——换一条新连接重发整
 * 请求；与用户主动取消（AbortError，不可重试）严格区分。
 */
class StreamTimeoutError extends Error {
    constructor(phase, timeoutMs) {
        super(`stream ${phase} timeout after ${timeoutMs}ms`);
        this.phase = phase;
        this.timeoutMs = timeoutMs;
        this.name = 'StreamTimeoutError';
    }
}
exports.StreamTimeoutError = StreamTimeoutError;
/**
 * 上游以 HTTP 200/SSE 正常结束，但缺少协议要求的终态事件/字段。
 * 这类结果不可信，但重发整次 buffered LLM 请求通常可以恢复。
 */
class StreamProtocolError extends Error {
    constructor(message) {
        super(message);
        this.name = 'StreamProtocolError';
    }
}
exports.StreamProtocolError = StreamProtocolError;
// OpenAI 风格错误体把 code 放在 `error.code`（如 `{error:{code,message,type}}`）；
// 仅少数上游用顶层 `code`。优先读嵌套，找不到再回退顶层，保证两种结构都能识别。
function extractBodyCode(body) {
    try {
        const obj = JSON.parse(body);
        if (obj && typeof obj === 'object') {
            const err = obj.error;
            if (err && typeof err === 'object') {
                const nestedCode = err.code;
                if (typeof nestedCode === 'string')
                    return nestedCode;
            }
            const topCode = obj.code;
            if (typeof topCode === 'string')
                return topCode;
            // OpenAI 风格永久错误（如 invalid_request_error）只在 error.type 给判别符，不带 code。
            // 黑名单里就列了 invalid_request_error，必须把 type 也纳入识别——否则 HTTP 400
            // invalid_request_error 会落到状态码默认重试，白烧整轮时间预算（见 deepseek 模型打到
            // Codex 端点的 400 案例）。优先级最低：code 命中时不会走到这里。
            if (err && typeof err === 'object') {
                const nestedType = err.type;
                if (typeof nestedType === 'string')
                    return nestedType;
            }
        }
    }
    catch { /* not JSON */ }
    return null;
}
function isRetryableStatus(status) {
    if (status < 400 || status >= 600)
        return false;
    return !NON_RETRYABLE_HTTP_STATUS.has(status);
}
/**
 * 是否属于过载/限流类错误，需要走指数退避。包含：
 *   - HTTP 429（标准限流）
 *   - HttpResponseError body code 命中 OVERLOADED_BODY_CODES（如 server_is_overloaded 走 HTTP 400）
 *   - SDK error 自带 status === 429
 */
function isOverloadedError(err) {
    if (!(err instanceof Error))
        return false;
    if (err instanceof HttpResponseError) {
        if (err.status === 429)
            return true;
        if (err.bodyCode && OVERLOADED_BODY_CODES.has(err.bodyCode))
            return true;
        return false;
    }
    const sdkStatus = err.status;
    if (sdkStatus === 429)
        return true;
    return false;
}
/**
 * Detect provider errors that mean the assembled prompt exceeded the model's
 * context window. Crabot uses this to compact context instead of retrying the
 * same oversized request.
 */
function isContextWindowError(err) {
    if (!(err instanceof Error))
        return false;
    const body = err instanceof HttpResponseError ? err.body : '';
    const message = `${err.message || ''} ${body || ''}`.toLowerCase();
    return [
        'context_length_exceeded',
        'context window',
        'maximum context length',
        'max context length',
        'too many tokens',
        'prompt is too long',
        'input is too long',
    ].some((pattern) => message.includes(pattern));
}
/**
 * Detect connection-level errors that can be recovered by retrying on a fresh
 * connection. This complements isRetryableError for mid-stream recovery paths.
 */
function isConnectionRecoveryError(err) {
    if (!(err instanceof Error))
        return false;
    const code = err.code;
    if (typeof code === 'string' && RETRYABLE_CODES.has(code))
        return true;
    const message = `${err.message || ''}`.toLowerCase();
    return [
        'fetch failed', 'terminated', 'socket hang up', 'network error',
        'connection reset', 'connection refused', 'unexpected end', 'eof',
        'broken pipe', 'connection closed',
    ].some((pattern) => message.includes(pattern));
}
/**
 * Detect 429/overload errors that do not carry a Retry-After header. These
 * need their own retry budget to avoid infinite loops against providers that
 * always return 429 without guidance.
 */
function isOverloadedWithoutRetryAfter(err) {
    if (!(err instanceof Error))
        return false;
    return isOverloadedError(err) && getRetryAfterMs(err) === undefined;
}
/**
 * Extract Retry-After value in milliseconds from error response headers or
 * body. Returns undefined if no valid Retry-After is present.
 */
function getRetryAfterMs(err) {
    if (!(err instanceof Error))
        return undefined;
    const response = err.response;
    if (response && typeof response === 'object' && response.headers) {
        const retryAfter = response.headers['retry-after'];
        if (typeof retryAfter === 'string' && retryAfter !== '') {
            const seconds = parseInt(retryAfter, 10);
            if (!isNaN(seconds))
                return seconds * 1000;
        }
    }
    if (err instanceof HttpResponseError && err.body) {
        try {
            const obj = JSON.parse(err.body);
            if (obj && typeof obj === 'object') {
                const errorObj = obj.error;
                if (errorObj && typeof errorObj === 'object') {
                    if (typeof errorObj.retry_after_ms === 'number')
                        return errorObj.retry_after_ms;
                    if (typeof errorObj.retry_after === 'number')
                        return errorObj.retry_after * 1000;
                }
            }
        }
        catch { /* not JSON */ }
    }
    return undefined;
}
/**
 * Compute delay for connection recovery attempts. Uses exponential backoff
 * starting at 500ms, capped at 30 seconds.
 */
function computeConnectionRecoveryDelayMs(recoveryAttempt) {
    return Math.min(500 * Math.pow(2, recoveryAttempt), 30000);
}
/**
 * 计算单次重试前的等待时间。
 *   - useBackoff=false：固定 baseDelayMs
 *   - useBackoff=true：base * 2^attempt（cap 在 BACKOFF_MAX_DELAY_MS）
 *   - retryAfterMs：若上游显式返回 Retry-After（毫秒），优先使用
 *
 * attempt 为 0-indexed —— 第一次失败时 attempt=0，对应 base * 1。
 */
function computeRetryDelayMs(attempt, baseDelayMs, useBackoff, retryAfterMs) {
    if (retryAfterMs !== undefined && retryAfterMs > 0)
        return retryAfterMs;
    if (!useBackoff)
        return baseDelayMs;
    return Math.min(baseDelayMs * Math.pow(2, attempt), exports.BACKOFF_MAX_DELAY_MS);
}
function isRetryableError(err) {
    if (!(err instanceof Error))
        return false;
    if (err.name === 'AbortError')
        return false;
    // 流式超时（TTFB / 空闲）总是可重试——换新连接重发
    if (err instanceof StreamTimeoutError)
        return true;
    if (err instanceof StreamProtocolError)
        return true;
    if (err instanceof HttpResponseError) {
        // 先短路 body code 黑名单（如 content_filter）—— 这类错误即使包在 5xx 里也不该重试
        if (err.bodyCode && NON_RETRYABLE_BODY_CODES.has(err.bodyCode))
            return false;
        return isRetryableStatus(err.status);
    }
    // SDK errors (@anthropic-ai/sdk, openai sdk) expose .status as a number.
    const sdkStatus = err.status;
    if (typeof sdkStatus === 'number') {
        // status === 0 typically means "no response / connection failure" (retryable)
        if (sdkStatus === 0)
            return true;
        return isRetryableStatus(sdkStatus);
    }
    // SDK connection errors (no status, but distinctive name)
    if (err.name === 'APIConnectionError' || err.name === 'APIConnectionTimeoutError') {
        return true;
    }
    // walk cause chain for a known network error code
    const seen = new Set();
    let cur = err;
    while (cur instanceof Error && !seen.has(cur)) {
        seen.add(cur);
        const code = cur.code;
        if (typeof code === 'string' && RETRYABLE_CODES.has(code))
            return true;
        cur = cur.cause;
    }
    // Last resort: match generic undici message strings
    return RETRYABLE_MESSAGE_PATTERNS.some((p) => err.message.includes(p));
}
function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(new DOMException('Aborted', 'AbortError'));
            return;
        }
        const timer = setTimeout(() => {
            signal?.removeEventListener('abort', onAbort);
            resolve();
        }, ms);
        const onAbort = () => {
            clearTimeout(timer);
            reject(new DOMException('Aborted', 'AbortError'));
        };
        signal?.addEventListener('abort', onAbort, { once: true });
    });
}
/**
 * Sleep that can be interrupted by abort signal or config change signal.
 * When configChangedSignal fires, optionally run onConfigChanged and resolve.
 */
function interruptibleSleep(ms, { abortSignal, configChangedSignal, onConfigChanged } = {}) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            cleanup();
            resolve();
        }, ms);
        const cleanup = () => {
            clearTimeout(timer);
            abortSignal?.removeEventListener('abort', onAbort);
            configChangedSignal?.removeEventListener('abort', onConfigChange);
        };
        const onAbort = () => {
            cleanup();
            reject(new DOMException('Aborted', 'AbortError'));
        };
        const onConfigChange = () => {
            cleanup();
            if (onConfigChanged) {
                onConfigChanged().then(resolve, reject);
            }
            else {
                resolve();
            }
        };
        abortSignal?.addEventListener('abort', onAbort, { once: true });
        configChangedSignal?.addEventListener('abort', onConfigChange, { once: true });
    });
}
/**
 * Wraps a promise-returning factory with retry semantics. Retries on known
 * network / HTTP 5xx / 429 errors; gives up on AbortError and non-retryable
 * errors immediately.
 */
async function withRetry(label, fn, options = {}) {
    const maxRetries = options.maxRetries ?? exports.DEFAULT_MAX_RETRIES;
    const delayMs = options.delayMs ?? exports.DEFAULT_RETRY_DELAY_MS;
    const abortSignal = options.abortSignal;
    for (let attempt = 0;; attempt++) {
        try {
            return await fn();
        }
        catch (err) {
            if (abortSignal?.aborted)
                throw err;
            if (!isRetryableError(err))
                throw err;
            if (attempt >= maxRetries)
                throw err;
            const actualDelay = computeRetryDelayMs(attempt, delayMs, true);
            console.error(`[${label}] attempt ${attempt + 1} failed, retrying in ${actualDelay}ms (backoff):`, err);
            try {
                options.onRetry?.({
                    attempt: attempt + 1,
                    maxAttempts: maxRetries + 1,
                    delayMs: actualDelay,
                    error: err instanceof Error ? err : new Error(String(err)),
                });
            }
            catch { /* observability callback must not break retry */ }
            await sleep(actualDelay, abortSignal);
        }
    }
}
/**
 * Wraps an async generator factory with retry semantics.
 * Retries are only attempted BEFORE the first *material* chunk is yielded;
 * once a material chunk has been forwarded to the consumer, errors propagate
 * (partial output cannot be safely replayed).
 * Uses capped exponential backoff for all retryable errors.
 */
async function* streamWithRetry(label, makeStream, options = {}) {
    const maxRetries = options.maxRetries ?? exports.DEFAULT_MAX_RETRIES;
    const delayMs = options.delayMs ?? exports.DEFAULT_RETRY_DELAY_MS;
    const abortSignal = options.abortSignal;
    const isMaterial = options.isMaterial ?? (() => true);
    for (let attempt = 0;; attempt++) {
        let materialYielded = false;
        try {
            for await (const chunk of makeStream()) {
                if (!materialYielded && isMaterial(chunk)) {
                    materialYielded = true;
                }
                yield chunk;
            }
            return;
        }
        catch (err) {
            if (materialYielded)
                throw err;
            if (abortSignal?.aborted)
                throw err;
            if (!isRetryableError(err))
                throw err;
            if (attempt >= maxRetries)
                throw err;
            const actualDelay = computeRetryDelayMs(attempt, delayMs, true);
            console.error(`[${label}] attempt ${attempt + 1} failed, retrying in ${actualDelay}ms (backoff):`, err);
            try {
                options.onRetry?.({
                    attempt: attempt + 1,
                    maxAttempts: maxRetries + 1,
                    delayMs: actualDelay,
                    error: err instanceof Error ? err : new Error(String(err)),
                });
            }
            catch { /* observability callback must not break retry */ }
            await sleep(actualDelay, abortSignal);
        }
    }
}
//# sourceMappingURL=retry-utils.js.map
