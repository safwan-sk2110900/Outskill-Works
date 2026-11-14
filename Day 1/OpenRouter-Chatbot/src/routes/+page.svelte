<script lang="ts">
	import { onMount, tick } from 'svelte';

	// ---------- Types ----------
	type Role = 'system' | 'user' | 'assistant';
	type Msg = { id: string; role: Role; content: string; error?: string };
	type Segment = { kind: 'text' | 'code'; lang: string; body: string };

	// ---------- Config ----------
	// Override with VITE_API_URL in .env (e.g. VITE_API_URL=http://127.0.0.1:8000)
	const API_URL: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000';
	const STORAGE_KEY = 'chat-settings-v1';
	const MODEL_PRESETS = [
		'gemini-2.5-flash',
		'gemini-2.5-pro',
		'google/gemini-2.5-flash',
		'openai/gpt-4o'
	];
	const SUGGESTIONS = [
		'Explain server-sent events in plain terms',
		'Write a Python function that retries a failed request',
		'Give me a checklist for reviewing a pull request'
	];

	// ---------- State ----------
	let messages = $state<Msg[]>([]);
	let input = $state('');
	let streaming = $state(false);
	let showSettings = $state(false);
	let copiedId = $state<string | null>(null);

	let model = $state('gemini-2.5-flash');
	let temperature = $state(0.7);
	let maxTokens = $state(2048);
	let systemPrompt = $state('');

	let controller: AbortController | null = null;
	let scroller: HTMLElement;
	let composer: HTMLTextAreaElement;
	let pinnedToBottom = true;

	// ---------- Persistence ----------
	onMount(() => {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			if (raw) {
				const s = JSON.parse(raw);
				model = s.model ?? model;
				temperature = s.temperature ?? temperature;
				maxTokens = s.maxTokens ?? maxTokens;
				systemPrompt = s.systemPrompt ?? systemPrompt;
			}
		} catch {
			/* storage unavailable or corrupt: use defaults */
		}
		composer?.focus();
	});

	$effect(() => {
		const snapshot = { model, temperature, maxTokens, systemPrompt };
		try {
			localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
		} catch {
			/* ignore */
		}
	});

	// ---------- Helpers ----------
	const uid = () => crypto.randomUUID();

	function onScroll() {
		const gap = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
		pinnedToBottom = gap < 80;
	}

	async function scrollToBottom(force = false) {
		await tick();
		if (scroller && (force || pinnedToBottom)) {
			scroller.scrollTo({ top: scroller.scrollHeight });
		}
	}

	function resizeComposer() {
		if (!composer) return;
		composer.style.height = 'auto';
		composer.style.height = Math.min(composer.scrollHeight, 200) + 'px';
	}

	// Splits text into prose and fenced code. Handles a fence that is still open mid-stream.
	function parseSegments(text: string): Segment[] {
		const out: Segment[] = [];
		const re = /```([\w+-]*)\n?([\s\S]*?)(?:```|$)/g;
		let last = 0;
		let m: RegExpExecArray | null;
		while ((m = re.exec(text)) !== null) {
			if (m.index > last) out.push({ kind: 'text', lang: '', body: text.slice(last, m.index) });
			out.push({ kind: 'code', lang: m[1], body: m[2].replace(/\n$/, '') });
			last = re.lastIndex;
			if (m[0] === '') re.lastIndex++;
		}
		if (last < text.length) out.push({ kind: 'text', lang: '', body: text.slice(last) });
		return out;
	}

	async function copy(text: string, id: string) {
		try {
			await navigator.clipboard.writeText(text);
			copiedId = id;
			setTimeout(() => copiedId === id && (copiedId = null), 1500);
		} catch {
			/* clipboard blocked */
		}
	}

	function describeHttpError(status: number, body: any): string {
		const detail = body?.detail;
		if (typeof detail === 'string') return detail;
		if (Array.isArray(detail) && detail.length) {
			return detail.map((d: any) => d.msg ?? JSON.stringify(d)).join('; ');
		}
		return `The server responded with status ${status}.`;
	}

	// ---------- Streaming ----------
	async function streamReply(history: Msg[], target: Msg) {
		controller = new AbortController();
		streaming = true;

		const payload = {
			messages: [
				...(systemPrompt.trim() ? [{ role: 'system', content: systemPrompt.trim() }] : []),
				...history.filter((m) => m.content.trim()).map(({ role, content }) => ({ role, content }))
			],
			model: model.trim() || 'gemini-2.5-flash',
			temperature,
			max_tokens: maxTokens
		};

		try {
			const res = await fetch(`${API_URL}/api/chat/stream`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload),
				signal: controller.signal
			});

			if (!res.ok || !res.body) {
				const body = await res.json().catch(() => null);
				throw new Error(describeHttpError(res.status, body));
			}

			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buffer = '';
			let finished = false;

			while (!finished) {
				const { value, done } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

				let sep: number;
				while ((sep = buffer.indexOf('\n\n')) !== -1) {
					const block = buffer.slice(0, sep);
					buffer = buffer.slice(sep + 2);

					let event = 'message';
					const dataLines: string[] = [];
					for (const line of block.split('\n')) {
						if (line.startsWith('event:')) event = line.slice(6).trim();
						else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
					}
					const data = dataLines.join('\n');
					if (!data) continue;
					if (data === '[DONE]') {
						finished = true;
						break;
					}

					const parsed = JSON.parse(data);
					if (event === 'error') throw new Error(parsed.error ?? 'Unknown server error');
					if (parsed.delta) {
						target.content += parsed.delta;
						scrollToBottom();
					}
				}
			}
		} catch (err) {
			if ((err as Error).name === 'AbortError') {
				// User stopped generation: keep whatever arrived.
			} else if (err instanceof TypeError) {
				target.error = `Can't reach the backend at ${API_URL}. Check that it's running.`;
			} else {
				target.error = (err as Error).message;
			}
		} finally {
			streaming = false;
			controller = null;
			scrollToBottom();
			composer?.focus();
		}
	}

	async function send(text = input) {
		const content = text.trim();
		if (!content || streaming) return;

		input = '';
		await tick();
		resizeComposer();

		messages.push({ id: uid(), role: 'user', content });
		messages.push({ id: uid(), role: 'assistant', content: '' });
		const history = messages.slice(0, -1);
		const target = messages[messages.length - 1];

		pinnedToBottom = true;
		await scrollToBottom(true);
		await streamReply(history, target);
	}

	async function regenerate() {
		if (streaming) return;
		const lastAssistant = messages.findLastIndex((m) => m.role === 'assistant');
		if (lastAssistant === -1) return;
		messages.splice(lastAssistant, 1);
		messages.push({ id: uid(), role: 'assistant', content: '' });
		const history = messages.slice(0, -1);
		const target = messages[messages.length - 1];
		pinnedToBottom = true;
		await streamReply(history, target);
	}

	function stop() {
		controller?.abort();
	}

	function clearChat() {
		stop();
		messages = [];
		composer?.focus();
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
			e.preventDefault();
			send();
		}
	}

	const canRegenerate = $derived(
		!streaming && messages.length > 0 && messages[messages.length - 1].role === 'assistant'
	);
</script>

<svelte:head>
	<title>Chat</title>
</svelte:head>

<div class="app">
	<header class="bar">
		<div class="brand">
			<span class="mark" aria-hidden="true"></span>
			<h1>Chat</h1>
			<span class="model-tag" title="Active model">{model}</span>
		</div>
		<div class="bar-actions">
			<button class="ghost" onclick={clearChat} disabled={messages.length === 0}>
				New chat
			</button>
			<button
				class="ghost"
				aria-expanded={showSettings}
				aria-controls="settings"
				onclick={() => (showSettings = !showSettings)}
			>
				Settings
			</button>
		</div>
	</header>

	<div class="body">
		<main class="thread" bind:this={scroller} onscroll={onScroll} aria-live="polite">
			{#if messages.length === 0}
				<div class="empty">
					<h2>What are you working on?</h2>
					<p>Ask a question or pick a starting point.</p>
					<ul>
						{#each SUGGESTIONS as s}
							<li><button class="suggestion" onclick={() => send(s)}>{s}</button></li>
						{/each}
					</ul>
				</div>
			{:else}
				<div class="column">
					{#each messages as m, i (m.id)}
						<article class="msg {m.role}">
							<div class="who">{m.role === 'user' ? 'You' : 'Assistant'}</div>
							<div class="content">
								{#each parseSegments(m.content) as seg}
									{#if seg.kind === 'code'}
										<div class="code">
											<div class="code-head">
												<span>{seg.lang || 'code'}</span>
												<button class="mini" onclick={() => copy(seg.body, m.id + seg.body.length)}>
													{copiedId === m.id + seg.body.length ? 'Copied' : 'Copy'}
												</button>
											</div>
											<pre><code>{seg.body}</code></pre>
										</div>
									{:else}
										<p class="prose">{seg.body}</p>
									{/if}
								{/each}

								{#if m.role === 'assistant' && streaming && i === messages.length - 1 && !m.error}
									<span class="cursor" aria-label="Generating"></span>
								{/if}

								{#if m.error}
									<div class="error" role="alert">
										<strong>Request failed.</strong>
										{m.error}
									</div>
								{/if}
							</div>

							{#if m.role === 'assistant' && m.content && !(streaming && i === messages.length - 1)}
								<div class="tools">
									<button class="mini" onclick={() => copy(m.content, m.id)}>
										{copiedId === m.id ? 'Copied' : 'Copy'}
									</button>
									{#if canRegenerate && i === messages.length - 1}
										<button class="mini" onclick={regenerate}>Regenerate</button>
									{/if}
								</div>
							{/if}
						</article>
					{/each}

					{#if canRegenerate && messages[messages.length - 1].error}
						<div class="retry"><button class="mini" onclick={regenerate}>Try again</button></div>
					{/if}
				</div>
			{/if}
		</main>

		{#if showSettings}
			<aside class="settings" id="settings" aria-label="Model settings">
				<div class="settings-head">
					<h2>Settings</h2>
					<button class="mini" onclick={() => (showSettings = false)}>Close</button>
				</div>

				<label>
					<span>Model</span>
					<input list="models" bind:value={model} spellcheck="false" />
					<datalist id="models">
						{#each MODEL_PRESETS as p}<option value={p}></option>{/each}
					</datalist>
					<small>Use provider-prefixed names like <code>openai/gpt-4o</code> on OpenRouter.</small>
				</label>

				<label>
					<span>Temperature <output>{temperature.toFixed(1)}</output></span>
					<input type="range" min="0" max="2" step="0.1" bind:value={temperature} />
					<small>Lower is more focused. Higher is more varied.</small>
				</label>

				<label>
					<span>Max response tokens</span>
					<input type="number" min="1" step="128" bind:value={maxTokens} />
				</label>

				<label>
					<span>System prompt</span>
					<textarea
						rows="5"
						bind:value={systemPrompt}
						placeholder="Optional. Sets the assistant's behavior for every message."
					></textarea>
				</label>

				<p class="endpoint">Backend: <code>{API_URL}</code></p>
			</aside>
		{/if}
	</div>

	<footer class="composer-wrap">
		<div class="composer">
			<textarea
				bind:this={composer}
				bind:value={input}
				oninput={resizeComposer}
				onkeydown={onKeydown}
				rows="1"
				placeholder="Message the assistant"
				aria-label="Message"
			></textarea>
			{#if streaming}
				<button class="send stop" onclick={stop}>Stop</button>
			{:else}
				<button class="send" onclick={() => send()} disabled={!input.trim()}>Send</button>
			{/if}
		</div>
		<p class="hint">Enter to send, Shift+Enter for a new line</p>
	</footer>
</div>

<style>
	:global(html, body) {
		margin: 0;
		height: 100%;
	}

	.app {
		--bg: #eef1f5;
		--surface: #ffffff;
		--sunken: #e4e9f0;
		--ink: #1a2230;
		--muted: #5c687a;
		--line: #d3dae4;
		--accent: #2f4fd8;
		--accent-ink: #ffffff;
		--danger-bg: #fdecec;
		--danger-ink: #9b1c1c;
		--code-bg: #161c28;
		--code-ink: #dbe3f0;
		--font: 'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif;
		--mono: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;

		display: flex;
		flex-direction: column;
		height: 100dvh;
		background: var(--bg);
		color: var(--ink);
		font-family: var(--font);
		font-size: 16px;
		line-height: 1.55;
	}

	@media (prefers-color-scheme: dark) {
		.app {
			--bg: #12161e;
			--surface: #1b212c;
			--sunken: #232b38;
			--ink: #e6ebf3;
			--muted: #97a3b6;
			--line: #2d3645;
			--accent: #7d97ff;
			--accent-ink: #0d1220;
			--danger-bg: #3a1d20;
			--danger-ink: #ffb4b4;
			--code-bg: #0c1018;
		}
	}

	button,
	input,
	textarea {
		font: inherit;
		color: inherit;
	}

	button:focus-visible,
	input:focus-visible,
	textarea:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	/* Top bar */
	.bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 20px;
		background: var(--surface);
		border-bottom: 1px solid var(--line);
	}
	.brand {
		display: flex;
		align-items: center;
		gap: 10px;
		min-width: 0;
	}
	.brand h1 {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 650;
		letter-spacing: -0.01em;
	}
	.mark {
		width: 14px;
		height: 14px;
		border-radius: 4px 10px 4px 10px;
		background: var(--accent);
	}
	.model-tag {
		padding: 2px 8px;
		border-radius: 6px;
		background: var(--sunken);
		color: var(--muted);
		font-family: var(--mono);
		font-size: 0.78rem;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 40vw;
	}
	.bar-actions {
		display: flex;
		gap: 6px;
	}
	.ghost {
		padding: 6px 12px;
		border: 1px solid transparent;
		border-radius: 8px;
		background: transparent;
		cursor: pointer;
	}
	.ghost:hover:not(:disabled) {
		background: var(--sunken);
	}
	.ghost[aria-expanded='true'] {
		background: var(--sunken);
		border-color: var(--line);
	}
	.ghost:disabled {
		color: var(--muted);
		opacity: 0.55;
		cursor: default;
	}

	/* Layout */
	.body {
		flex: 1;
		display: flex;
		min-height: 0;
	}
	.thread {
		flex: 1;
		overflow-y: auto;
		padding: 24px 20px 12px;
	}
	.column {
		max-width: 760px;
		margin: 0 auto;
		display: flex;
		flex-direction: column;
		gap: 26px;
	}

	/* Empty state */
	.empty {
		max-width: 560px;
		margin: 12vh auto 0;
	}
	.empty h2 {
		margin: 0 0 6px;
		font-size: 1.75rem;
		letter-spacing: -0.02em;
	}
	.empty p {
		margin: 0 0 22px;
		color: var(--muted);
	}
	.empty ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.suggestion {
		width: 100%;
		text-align: left;
		padding: 12px 14px;
		border: 1px solid var(--line);
		border-radius: 10px;
		background: var(--surface);
		cursor: pointer;
	}
	.suggestion:hover {
		border-color: var(--accent);
	}

	/* Messages */
	.who {
		margin-bottom: 4px;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--muted);
	}
	.msg.user .content {
		padding: 10px 14px;
		border-radius: 12px 12px 4px 12px;
		background: var(--accent);
		color: var(--accent-ink);
		width: fit-content;
		max-width: 88%;
		margin-left: auto;
	}
	.msg.user .who {
		text-align: right;
	}
	.msg.assistant .content {
		max-width: 68ch;
	}
	.prose {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.content > * + * {
		margin-top: 10px;
	}

	.code {
		border-radius: 10px;
		overflow: hidden;
		background: var(--code-bg);
		color: var(--code-ink);
	}
	.code-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 4px 6px 4px 12px;
		font-family: var(--mono);
		font-size: 0.76rem;
		color: #8b98ad;
		border-bottom: 1px solid #262e3d;
	}
	.code pre {
		margin: 0;
		padding: 12px 14px;
		overflow-x: auto;
		font-family: var(--mono);
		font-size: 0.86rem;
		line-height: 1.5;
	}
	.code .mini {
		color: #b7c2d6;
	}
	.code .mini:hover {
		background: #262e3d;
	}

	.cursor {
		display: inline-block;
		width: 8px;
		height: 1.05em;
		vertical-align: text-bottom;
		border-radius: 2px;
		background: var(--accent);
		animation: blink 1s steps(2, start) infinite;
	}
	@keyframes blink {
		to {
			visibility: hidden;
		}
	}

	.tools,
	.retry {
		display: flex;
		gap: 4px;
		margin-top: 6px;
	}
	.mini {
		padding: 3px 8px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted);
		font-size: 0.8rem;
		cursor: pointer;
	}
	.mini:hover {
		background: var(--sunken);
		color: var(--ink);
	}

	.error {
		padding: 10px 12px;
		border-radius: 8px;
		background: var(--danger-bg);
		color: var(--danger-ink);
		font-size: 0.92rem;
	}

	/* Settings */
	.settings {
		width: 320px;
		flex-shrink: 0;
		overflow-y: auto;
		padding: 18px;
		background: var(--surface);
		border-left: 1px solid var(--line);
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.settings-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}
	.settings h2 {
		margin: 0;
		font-size: 1rem;
	}
	.settings label {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.settings label > span {
		display: flex;
		justify-content: space-between;
		font-size: 0.88rem;
		font-weight: 600;
	}
	.settings output {
		font-family: var(--mono);
		font-weight: 400;
		color: var(--muted);
	}
	.settings small {
		color: var(--muted);
		font-size: 0.78rem;
	}
	.settings input:not([type='range']),
	.settings textarea {
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg);
		resize: vertical;
	}
	.settings input[type='range'] {
		accent-color: var(--accent);
	}
	.endpoint {
		margin: 0;
		color: var(--muted);
		font-size: 0.78rem;
		overflow-wrap: anywhere;
	}
	code {
		font-family: var(--mono);
		font-size: 0.92em;
	}

	/* Composer */
	.composer-wrap {
		padding: 8px 20px 14px;
	}
	.composer {
		max-width: 760px;
		margin: 0 auto;
		display: flex;
		align-items: flex-end;
		gap: 8px;
		padding: 8px;
		border: 1px solid var(--line);
		border-radius: 14px;
		background: var(--surface);
	}
	.composer:focus-within {
		border-color: var(--accent);
	}
	.composer textarea {
		flex: 1;
		padding: 6px 8px;
		border: 0;
		outline: 0;
		background: transparent;
		resize: none;
		max-height: 200px;
		line-height: 1.5;
	}
	.send {
		padding: 8px 18px;
		border: 0;
		border-radius: 10px;
		background: var(--accent);
		color: var(--accent-ink);
		font-weight: 600;
		cursor: pointer;
	}
	.send:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.send.stop {
		background: var(--ink);
		color: var(--surface);
	}
	.hint {
		max-width: 760px;
		margin: 6px auto 0;
		color: var(--muted);
		font-size: 0.75rem;
	}

	/* Small screens: settings become a sheet over the thread */
	@media (max-width: 760px) {
		.settings {
			position: fixed;
			inset: 52px 0 0 auto;
			width: min(340px, 92vw);
			z-index: 5;
			box-shadow: -8px 0 24px rgb(0 0 0 / 0.18);
		}
		.msg.user .content {
			max-width: 96%;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.cursor {
			animation: none;
		}
	}
</style>