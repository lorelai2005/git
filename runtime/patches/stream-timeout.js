"use strict";
/**
 * 流式超时原语：给任意 LLM 流加 TTFB（首 chunk）和空闲（相邻 chunk 间隔）两道超时。
 *
 * 背景：非流式 / 静默连接的请求，链路上的网关 / 反代可能在长时间无字节时把连接掐掉
 * （表现为 `UND_ERR_SOCKET: other side closed`）。流式让字节持续流动能缓解，但仍需
 * 主动超时来：① 首 chunk 迟迟不来时尽早 abort 换新连接（而不是干等到对端掐线）；
 * ② 流中途挂起（连接还在但不再吐 chunk）时及时止损。
 *
 * 超时触发的是内部 AbortController，并翻译成可重试的 StreamTimeoutError，与用户主动
 * 取消（userSignal）严格区分——后者原样抛出 AbortError，不可重试。
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.STREAM_IDLE_MS = exports.STREAM_TTFB_MS = void 0;
exports.withStreamTimeout = withStreamTimeout;
exports.streamWithTimeoutAndRetry = streamWithTimeoutAndRetry;
const retry_utils_js_1 = require("./retry-utils.js");
const stream_processor_js_1 = require("./stream-processor.js");
const llm_adapter_types_js_1 = require("./llm-adapter-types.js");
// 默认值偏保守，容得下慢 prefill 的大上下文请求；可用环境变量覆盖。
exports.STREAM_TTFB_MS = Number(process.env.CRABOT_STREAM_TTFB_MS) || 60000;
exports.STREAM_IDLE_MS = Number(process.env.CRABOT_STREAM_IDLE_MS) || 75000;
/**
 * 包装一个「按 signal 取消」的流工厂，加上 TTFB + 空闲超时。
 *
 * @param makeStream 接收组合后的 AbortSignal，返回单次流。超时会通过该 signal 中断底层请求。
 * @param userSignal 调用方的取消信号（task 级）。用户取消 → 原样 AbortError；超时 → StreamTimeoutError。
 */
async function* withStreamTimeout(makeStream, userSignal, opts = {}) {
    const ttfbMs = opts.ttfbMs ?? exports.STREAM_TTFB_MS;
    const idleMs = opts.idleMs ?? exports.STREAM_IDLE_MS;
    // 不用 AbortSignal.any：它会把组合 signal 挂到长寿命的 task 级 userSignal 的 dependent
    // 链上，只有 GC 才解开 —— 每次 LLM 调用都挂一个，长跑会把 ctrl/timer/stream 帧滞留到
    // native heap（正是 anthropic-adapter 那段手工 add/removeEventListener 要避免的泄漏，见
    // 2026-06-06 kernel watchdog 复盘）。改为手工桥接 + finally 确定性摘除。
    const ctrl = new AbortController();
    let onUserAbort = null;
    if (userSignal) {
        if (userSignal.aborted) {
            ctrl.abort();
        }
        else {
            onUserAbort = () => ctrl.abort();
            userSignal.addEventListener('abort', onUserAbort, { once: true });
        }
    }
    let timer;
    let phase = 'ttfb';
    let timedOut = false;
    const arm = (ms) => {
        if (timer)
            clearTimeout(timer);
        timer = setTimeout(() => {
            timedOut = true;
            ctrl.abort();
        }, ms);
        timer.unref?.();
    };
    try {
        arm(ttfbMs);
        for await (const chunk of makeStream(ctrl.signal)) {
            phase = 'idle';
            arm(idleMs);
            yield chunk;
        }
    }
    catch (err) {
        // 超时触发的 abort（且非用户取消）→ 翻译成可重试错误；用户取消原样抛出
        if (timedOut && !userSignal?.aborted) {
            throw new retry_utils_js_1.StreamTimeoutError(phase, phase === 'ttfb' ? ttfbMs : idleMs);
        }
        throw err;
    }
    finally {
        if (timer)
            clearTimeout(timer);
        if (userSignal && onUserAbort)
            userSignal.removeEventListener('abort', onUserAbort);
    }
}
/**
 * adapter.stream() 的统一封装：内层 = streamOnce + TTFB/空闲超时，外层 = pre-material 重试
 * （streamWithRetry，超时抛 StreamTimeoutError 被判定可重试 → 换新连接重发）。
 *
 * 三个 adapter 共用此封装，避免新 adapter 漏接超时或重试中的一半。
 * 超时的 abort 必须经 withStreamTimeout 注入的 signal 才能传到底层 fetch，所以封装在
 * 这一层（而非 adapter.stream() 外面）。
 */
async function* streamWithTimeoutAndRetry(label, streamOnce, params) {
    yield* (0, retry_utils_js_1.streamWithRetry)(label, () => withStreamTimeout((signal) => streamOnce({ ...params, signal }), params.signal), {
        abortSignal: params.signal,
        isMaterial: stream_processor_js_1.isMaterialChunk,
        onRetry: (0, llm_adapter_types_js_1.wrapOnRetry)(params.onRetry, 'pre-stream'),
    });
}
//# sourceMappingURL=stream-timeout.js.map