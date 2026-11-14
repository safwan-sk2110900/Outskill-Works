<script lang="ts">
	import { onMount } from 'svelte';

	type Status = 'not_started' | 'incomplete' | 'complete';
	type QuizSummary = {
		id: string;
		title: string;
		source_filename: string;
		created_at: string;
		status: Status;
		answered: number;
		total: number;
		score: number | null;
	};
	type Question = {
		question: string;
		options: string[];
		correct_index?: number; // only present when status is complete
		is_correct?: boolean;
	};
	type QuizDetail = QuizSummary & { questions: Question[]; answers: Record<number, number> };

	const API_URL: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000';
	const JSON_HEADERS = { 'Content-Type': 'application/json' };
	const STATUS_LABEL: Record<Status, string> = {
		not_started: 'Not started',
		incomplete: 'Incomplete',
		complete: 'Complete'
	};
	const ACTION_LABEL: Record<Status, string> = {
		not_started: 'Start',
		incomplete: 'Continue',
		complete: 'Review'
	};

	let quizzes = $state<QuizSummary[]>([]);
	let loadingList = $state(true);
	let listError = $state('');

	let uploading = $state(false);
	let uploadingName = $state('');
	let uploadError = $state('');
	let fileInput = $state<HTMLInputElement | null>(null);

	let active = $state<QuizDetail | null>(null);
	let answers = $state<Record<number, number>>({});
	let opening = $state(false);
	let submitting = $state(false);
	let quizError = $state('');

	const done = $derived(active?.status === 'complete');
	const answeredCount = $derived(Object.keys(answers).length);
	const percent = $derived(
		active && active.score !== null ? Math.round((active.score / active.total) * 100) : 0
	);

	// ---------- API ----------
	function describeHttpError(status: number, body: any): string {
		const detail = body?.detail;
		if (typeof detail === 'string') return detail;
		if (Array.isArray(detail) && detail.length) {
			return detail.map((d: any) => d.msg ?? JSON.stringify(d)).join('; ');
		}
		return `The server responded with status ${status}.`;
	}

	async function api<T>(path: string, init?: RequestInit): Promise<T> {
		let res: Response;
		try {
			res = await fetch(`${API_URL}${path}`, init);
		} catch {
			throw new Error(`Can't reach the backend at ${API_URL}. Check that it's running.`);
		}
		if (!res.ok) {
			const body = await res.json().catch(() => null);
			throw new Error(describeHttpError(res.status, body));
		}
		return res.json();
	}

	async function loadQuizzes() {
		listError = '';
		try {
			quizzes = await api<QuizSummary[]>('/api/quizzes');
		} catch (err) {
			listError = (err as Error).message;
		} finally {
			loadingList = false;
		}
	}

	onMount(loadQuizzes);

	// ---------- Upload ----------
	async function onFilePicked(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = ''; // lets you pick the same file twice
		if (!file) return;

		uploading = true;
		uploadingName = file.name;
		uploadError = '';
		try {
			const form = new FormData();
			form.append('file', file);
			// No Content-Type header here: the browser adds the multipart boundary itself.
			await api('/api/quizzes', { method: 'POST', body: form });
			await loadQuizzes();
		} catch (err) {
			uploadError = (err as Error).message;
		} finally {
			uploading = false;
		}
	}

	// ---------- Taking a quiz ----------
	async function openQuiz(id: string) {
		opening = true;
		quizError = '';
		try {
			const detail = await api<QuizDetail>(`/api/quizzes/${id}`);
			answers = { ...detail.answers };
			active = detail;
			window.scrollTo({ top: 0 });
		} catch (err) {
			listError = (err as Error).message;
		} finally {
			opening = false;
		}
	}

	async function choose(qIndex: number, optIndex: number) {
		if (!active || done) return;
		answers[qIndex] = optIndex;
		quizError = '';
		try {
			await api(`/api/quizzes/${active.id}/progress`, {
				method: 'PUT',
				headers: JSON_HEADERS,
				body: JSON.stringify({ answers: $state.snapshot(answers) })
			});
		} catch (err) {
			quizError = `Your last answer wasn't saved. ${(err as Error).message}`;
		}
	}

	async function submit() {
		if (!active || submitting) return;
		submitting = true;
		quizError = '';
		try {
			await api(`/api/quizzes/${active.id}/submit`, {
				method: 'POST',
				headers: JSON_HEADERS,
				body: JSON.stringify({ answers: $state.snapshot(answers) })
			});
			await openQuiz(active.id); // re-fetch: the server now includes correct answers
		} catch (err) {
			quizError = (err as Error).message;
		} finally {
			submitting = false;
		}
	}

	async function closeQuiz() {
		active = null;
		await loadQuizzes();
	}

	// ---------- Display helpers ----------
	function detail(q: QuizSummary): string {
		if (q.status === 'complete') return `Score ${q.score} of ${q.total}`;
		if (q.status === 'incomplete') return `${q.answered} of ${q.total} answered`;
		return `${q.total} questions`;
	}

	function formatDate(iso: string): string {
		return new Date(iso).toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}
</script>

<svelte:head>
	<title>Quiz Maker</title>
</svelte:head>

<div class="app">
	<header class="bar">
		<div class="brand">
			<span class="mark" aria-hidden="true"></span>
			<h1>Quiz Maker</h1>
		</div>
		{#if !active}
			<input bind:this={fileInput} type="file" hidden onchange={onFilePicked} />
			<button class="primary" onclick={() => fileInput?.click()} disabled={uploading}>
				Upload file
			</button>
		{/if}
	</header>

	<main class="page">
		{#if !active}
			<!-- ========== QUIZ LIST ========== -->
			{#if uploading}
				<div class="banner" role="status">
					<span class="spinner" aria-hidden="true"></span>
					Generating a quiz from <strong>{uploadingName}</strong>. This can take up to a minute.
				</div>
			{/if}
			{#if uploadError}
				<div class="banner error" role="alert">
					<strong>Couldn't create a quiz.</strong>
					{uploadError}
				</div>
			{/if}
			{#if listError}
				<div class="banner error" role="alert">{listError}</div>
			{/if}

			<section aria-labelledby="quizzes-heading">
				<h2 id="quizzes-heading">Quizzes</h2>

				{#if loadingList}
					<p class="muted">Loading quizzes...</p>
				{:else if quizzes.length === 0 && !listError}
					<div class="empty">
						<p><strong>No quizzes yet.</strong></p>
						<p class="muted">
							Upload a document, note or image and it becomes a 10-question quiz.
						</p>
					</div>
				{:else}
					<ul class="list">
						{#each quizzes as q (q.id)}
							<li>
								<button class="row" onclick={() => openQuiz(q.id)} disabled={opening}>
									<span class="row-main">
										<span class="row-title">{q.title}</span>
										<span class="row-sub">{q.source_filename}, {formatDate(q.created_at)}</span>
									</span>
									<span class="pill {q.status}">{STATUS_LABEL[q.status]}</span>
									<span class="row-detail">{detail(q)}</span>
									<span class="row-action">{ACTION_LABEL[q.status]}</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{:else}
			<!-- ========== QUIZ VIEW ========== -->
			<button class="back" onclick={closeQuiz}>Back to quizzes</button>

			<div class="quiz-head">
				<h2>{active.title}</h2>
				<p class="muted">{active.source_filename}</p>
			</div>

			{#if done}
				<div class="result" role="status">
					<div class="result-score">{active.score} / {active.total}</div>
					<div>
						<strong>{percent}%</strong>
						<p class="muted">
							{active.score === active.total
								? 'Perfect score.'
								: `${active.total - (active.score ?? 0)} answered incorrectly. They're flagged below.`}
						</p>
					</div>
				</div>
			{:else}
				<div class="progress" role="status">
					<div class="bar-track">
						<div class="bar-fill" style:width="{(answeredCount / active.total) * 100}%"></div>
					</div>
					<span>{answeredCount} of {active.total} answered</span>
				</div>
			{/if}

			{#if quizError}
				<div class="banner error" role="alert">{quizError}</div>
			{/if}

			<ol class="questions">
				{#each active.questions as q, i}
					<li>
						<fieldset class="question">
							<legend>
								<span class="qnum">Question {i + 1}</span>
								{#if done}
									<span class="verdict" class:bad={!q.is_correct}>
										{q.is_correct ? 'Correct' : 'Incorrect'}
									</span>
								{/if}
							</legend>
							<p class="qtext">{q.question}</p>

							{#each q.options as opt, o}
								{@const picked = answers[i] === o}
								{@const isRight = done && q.correct_index === o}
								{@const isWrong = done && picked && q.correct_index !== o}
								<label class="opt" class:picked class:right={isRight} class:wrong={isWrong}>
									<input
										type="radio"
										name={`q${i}`}
										checked={picked}
										disabled={done}
										onchange={() => choose(i, o)}
									/>
									<span class="letter">{'ABCD'[o]}</span>
									<span class="opt-text">{opt}</span>
									{#if isRight}<span class="flag">Correct answer</span>{/if}
									{#if isWrong}<span class="flag">Your answer</span>{/if}
								</label>
							{/each}
						</fieldset>
					</li>
				{/each}
			</ol>

			<div class="footer">
				{#if done}
					<button class="primary" onclick={closeQuiz}>Back to quizzes</button>
				{:else}
					<button
						class="primary"
						onclick={submit}
						disabled={answeredCount < active.total || submitting}
					>
						{submitting ? 'Submitting...' : 'Submit quiz'}
					</button>
					{#if answeredCount < active.total}
						<span class="muted">Answer all {active.total} questions to submit.</span>
					{/if}
				{/if}
			</div>
		{/if}
	</main>
</div>

<style>
	:global(html, body) {
		margin: 0;
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
		--ok-bg: #e0f3e6;
		--ok-ink: #14532d;
		--warn-bg: #fdf0d5;
		--warn-ink: #7a4b00;
		--bad-bg: #fdecec;
		--bad-ink: #9b1c1c;
		--font: 'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif;

		min-height: 100dvh;
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
			--ok-bg: #163524;
			--ok-ink: #9be3b3;
			--warn-bg: #3b2f14;
			--warn-ink: #f2cf85;
			--bad-bg: #3a1d20;
			--bad-ink: #ffb4b4;
		}
	}

	button,
	input {
		font: inherit;
		color: inherit;
	}
	button:focus-visible,
	.opt:has(input:focus-visible) {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	h2 {
		margin: 0 0 12px;
		font-size: 1.25rem;
		letter-spacing: -0.01em;
	}
	.muted {
		color: var(--muted);
		margin: 0;
	}

	/* Header */
	.bar {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 20px;
		background: var(--surface);
		border-bottom: 1px solid var(--line);
	}
	.brand {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.brand h1 {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 650;
	}
	.mark {
		width: 14px;
		height: 14px;
		border-radius: 4px 10px 4px 10px;
		background: var(--accent);
	}
	.primary {
		padding: 8px 18px;
		border: 0;
		border-radius: 10px;
		background: var(--accent);
		color: var(--accent-ink);
		font-weight: 600;
		cursor: pointer;
	}
	.primary:disabled {
		opacity: 0.45;
		cursor: default;
	}

	.page {
		max-width: 760px;
		margin: 0 auto;
		padding: 24px 20px 64px;
	}

	/* Banners */
	.banner {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-bottom: 16px;
		padding: 10px 14px;
		border-radius: 10px;
		background: var(--surface);
		border: 1px solid var(--line);
	}
	.banner.error {
		background: var(--bad-bg);
		border-color: transparent;
		color: var(--bad-ink);
	}
	.spinner {
		flex-shrink: 0;
		width: 16px;
		height: 16px;
		border: 2px solid var(--line);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* List */
	.empty {
		padding: 28px;
		border: 1px dashed var(--line);
		border-radius: 12px;
		text-align: center;
	}
	.empty p {
		margin: 0 0 4px;
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.row {
		width: 100%;
		display: grid;
		grid-template-columns: 1fr auto auto auto;
		align-items: center;
		gap: 14px;
		padding: 14px 16px;
		text-align: left;
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: 12px;
		cursor: pointer;
	}
	.row:hover:not(:disabled) {
		border-color: var(--accent);
	}
	.row-main {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.row-title {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-sub,
	.row-detail {
		color: var(--muted);
		font-size: 0.85rem;
	}
	.row-action {
		font-weight: 600;
		color: var(--accent);
		min-width: 4.5rem;
		text-align: right;
	}
	.pill {
		padding: 2px 10px;
		border-radius: 999px;
		font-size: 0.8rem;
		font-weight: 600;
		background: var(--sunken);
		color: var(--muted);
	}
	.pill.incomplete {
		background: var(--warn-bg);
		color: var(--warn-ink);
	}
	.pill.complete {
		background: var(--ok-bg);
		color: var(--ok-ink);
	}

	/* Quiz view */
	.back {
		margin-bottom: 14px;
		padding: 6px 10px;
		margin-left: -10px;
		border: 0;
		border-radius: 8px;
		background: transparent;
		color: var(--muted);
		cursor: pointer;
	}
	.back:hover {
		background: var(--sunken);
	}
	.quiz-head {
		margin-bottom: 18px;
	}
	.result {
		display: flex;
		align-items: center;
		gap: 18px;
		margin-bottom: 20px;
		padding: 16px 18px;
		border-radius: 12px;
		background: var(--ok-bg);
		color: var(--ok-ink);
	}
	.result .muted {
		color: inherit;
		opacity: 0.85;
	}
	.result-score {
		font-size: 2rem;
		font-weight: 700;
		letter-spacing: -0.02em;
	}
	.progress {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 20px;
		font-size: 0.9rem;
		color: var(--muted);
	}
	.bar-track {
		flex: 1;
		height: 6px;
		border-radius: 3px;
		background: var(--sunken);
		overflow: hidden;
	}
	.bar-fill {
		height: 100%;
		background: var(--accent);
		transition: width 0.2s;
	}

	.questions {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.question {
		margin: 0;
		padding: 16px 18px;
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: 12px;
	}
	legend {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 0 6px;
		margin-left: -6px;
	}
	.qnum {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--muted);
	}
	.verdict {
		padding: 1px 8px;
		border-radius: 6px;
		font-size: 0.8rem;
		font-weight: 600;
		background: var(--ok-bg);
		color: var(--ok-ink);
	}
	.verdict.bad {
		background: var(--bad-bg);
		color: var(--bad-ink);
	}
	.qtext {
		margin: 4px 0 12px;
		font-weight: 600;
	}
	.opt {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 8px;
		padding: 10px 12px;
		border: 1px solid var(--line);
		border-radius: 10px;
		cursor: pointer;
	}
	.opt input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}
	.opt:hover {
		border-color: var(--accent);
	}
	.opt.picked {
		border-color: var(--accent);
		background: var(--sunken);
	}
	.opt.right {
		border-color: transparent;
		background: var(--ok-bg);
		color: var(--ok-ink);
	}
	.opt.wrong {
		border-color: transparent;
		background: var(--bad-bg);
		color: var(--bad-ink);
	}
	.opt:has(input:disabled) {
		cursor: default;
	}
	.letter {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		border-radius: 50%;
		background: var(--sunken);
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--ink);
	}
	.opt-text {
		flex: 1;
	}
	.flag {
		font-size: 0.78rem;
		font-weight: 600;
	}
	.footer {
		display: flex;
		align-items: center;
		gap: 14px;
		margin-top: 24px;
	}

	@media (max-width: 620px) {
		.row {
			grid-template-columns: 1fr auto;
		}
		.row-detail {
			grid-column: 1;
		}
		.row-action {
			grid-column: 2;
			grid-row: 2;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
		.bar-fill {
			transition: none;
		}
	}
</style>