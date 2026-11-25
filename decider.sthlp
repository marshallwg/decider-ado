{smcl}
{* *! version 0.1.0  25nov2026}{...}
{viewerjumpto "Syntax" "decider##syntax"}{...}
{viewerjumpto "Description" "decider##description"}{...}
{viewerjumpto "Options" "decider##options"}{...}
{viewerjumpto "Generated variables" "decider##variables"}{...}
{viewerjumpto "Examples" "decider##examples"}{...}
{viewerjumpto "Stored results" "decider##results"}{...}
{title:Title}

{p2colset 5 18 20 2}{...}
{p2col :{bf:decider} {hline 2}}Structured probabilistic decisions over text variables{p_end}
{p2colreset}{...}


{marker syntax}{...}
{title:Syntax}

{p 8 16 2}
{cmd:decider} {varlist} {ifin} {cmd:,}
{opt t:ype(qtype)} {opt pre:fix(stub)}
[{it:options}]

{p 8 16 2}
{cmd:decider resume} {cmd:,} {opt run:dir(path)}
[{opt time:out(#)} {opt work:ers(#)} {opt py:thon(path)} {opt rep:lace}]

{p 8 16 2}
{cmd:decider check} [{cmd:,} {opt py:thon(path)} {opt ven:dordir(path)} {opt mod:el(string)}]

{p 8 16 2}
{cmd:decider setup} {cmd:,} {c -(}{opt loc:al} [{opt down:loadmodel}] | {opt open:router}{c )-}
[{opt mod:el(string)} {opt py:thon(path)}]

{pstd}
For step-by-step installation and backend setup instructions (both hosted
OpenRouter and local Apple Silicon NeoJev), run {cmd:decider check} and see
{browse "https://github.com/marshallwg/decider-ado#setup":github.com/marshallwg/decider-ado#setup}.

{synoptset 28 tabbed}{...}
{synopthdr}
{synoptline}
{syntab:Required}
{synopt :{opt t:ype(qtype)}}decision type: {cmd:choice}, {cmd:score}, or {cmd:noul}{p_end}
{synopt :{opt pre:fix(stub)}}prefix for generated variables{p_end}
{synopt :{opt prompt(string)}}inline prompt text (or statement for {cmd:noul}){p_end}
{synopt :{opt promptf:ile(filename)}}path to UTF-8 text/Markdown prompt file (alternative to {opt prompt()}){p_end}
{synopt :{opt opt:ions(string)}}quoted option labels for {cmd:choice} or ordered levels for {cmd:score} (2 to 16){p_end}

{syntab:Backend & privacy}
{synopt :{opt back:end(name)}}inference backend: {cmd:neojev} (default, local Apple Silicon MLX), {cmd:openrouter} (hosted Jev API), or {cmd:mock} (offline test backend){p_end}
{synopt :{opt allow:cloud}}required explicit opt-in when {opt backend(openrouter)} is selected{p_end}
{synopt :{opt mod:el(string)}}override default model identifier for the chosen backend{p_end}
{synopt :{opt py:thon(path)}}Python executable (default: {cmd:python3}; point to {cmd:.venv/bin/python} for local MLX){p_end}
{synopt :{opt ven:dordir(path)}}directory containing NeoJev's {cmd:decider.py} (or set {cmd:NEOJEV_VENDOR_DIR}){p_end}

{syntab:Quality control & execution}
{synopt :{opt rev:ersecheck}}rerun {cmd:choice}/{cmd:score} with reversed option order to test order stability{p_end}
{synopt :{opt minp:rob(#)}}flag {cmd:choice}/{cmd:score} rows with top probability below {it:#}; default {cmd:0.80}{p_end}
{synopt :{opt minm:argin(#)}}flag {cmd:choice}/{cmd:score} rows with top-minus-second margin below {it:#}; default {cmd:0.30}{p_end}
{synopt :{opt thr:eshold(#)}}flag {cmd:noul} rows with probability below {it:#}; default {cmd:0.80}{p_end}
{synopt :{opt run:dir(path)}}persistent directory for frozen inputs, checkpoints, and provenance logs{p_end}
{synopt :{opt work:ers(#)}}concurrent threads for {cmd:openrouter} (default {cmd:1}; must be {cmd:1} for {cmd:neojev}){p_end}
{synopt :{opt time:out(#)}}HTTP timeout in seconds for {cmd:openrouter}; default {cmd:60}{p_end}
{synopt :{opt token:budget(#)}}token budget for local {cmd:neojev}; default {cmd:4096}{p_end}
{synopt :{opt rep:lace}}overwrite existing {it:prefix}{cmd:_*} variables{p_end}
{synoptline}


{marker description}{...}
{title:Description}

{pstd}
{cmd:decider} applies a structured decision model to one or more string
variables in the active Stata dataset. When one variable is specified, its
text is passed directly as the state; when multiple variables are specified,
they are serialized as a JSON object keyed by variable name. Observations
with blank or missing text are not sent to the model and are flagged as
{cmd:missing_text}.

{pstd}
{cmd:decider} delegates inference to a bundled Python subprocess
({cmd:_decider_score.py}), checkpoints each completed observation by row ID,
and links results back into the active dataset via Stata frames. For
{opt backend(openrouter)}, the API key is read exclusively from the
{cmd:OPENROUTER_API_KEY} environment variable and is never written to disk.


{pstd}
When resuming with observations in memory, the original input text must remain
at every original sample position. Changed text, reordered observations, or
missing original positions stop resume before model calls or replacement of
results. Invalid or incomplete checkpoint rows also stop resume without
rescoring them. Restore the original inputs and order before retrying.
With no observations in memory, resume loads scores directly and returns only
{cmd:r(cmd)}, {cmd:r(subcmd)}, and {cmd:r(run_dir)}.

{marker variables}{...}
{title:Generated variables}

{pstd}
For {cmd:choice} and {cmd:score}:

{p 8 12 2}{it:stub}{cmd:_class} — selected option (labeled integer {cmd:1}..{it:K}){p_end}
{p 8 12 2}{it:stub}{cmd:_p1} .. {it:stub}{cmd:_pK} — probability for each option ({cmd:double}){p_end}
{p 8 12 2}{it:stub}{cmd:_top_p} — highest option probability{p_end}
{p 8 12 2}{it:stub}{cmd:_margin} — difference between highest and second-highest probabilities{p_end}
{p 8 12 2}{it:stub}{cmd:_expected} — expected level index on {cmd:1}..{it:K} ({cmd:score} only){p_end}
{p 8 12 2}{it:stub}{cmd:_rev_class}, {it:stub}{cmd:_order_stable} — reversed-order result and stability indicator (with {opt reversecheck}){p_end}
{p 8 12 2}{it:stub}{cmd:_status}, {it:stub}{cmd:_review}, {it:stub}{cmd:_review_reason} — status and review flags{p_end}

{pstd}
For {cmd:noul}:

{p 8 12 2}{it:stub}{cmd:_prob} — probability that the statement is true{p_end}
{p 8 12 2}{it:stub}{cmd:_status}, {it:stub}{cmd:_review}, {it:stub}{cmd:_review_reason}{p_end}


{marker examples}{...}
{title:Examples}

{pstd}Local Apple Silicon NeoJev classification with order-stability check:{p_end}
{phang2}{cmd:. decider goal_text session_title, type(choice) prefix(align) ///}{p_end}
{phang3}{cmd:promptfile("prompts/alignment_v2.md") ///}{p_end}
{phang3}{cmd:options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///}{p_end}
{phang3}{cmd:backend(neojev) python(".venv/bin/python") reversecheck rundir("runs/align_v2")}{p_end}

{pstd}Hosted OpenRouter classification with 4 concurrent workers:{p_end}
{phang2}{cmd:. decider response_text, type(noul) prefix(inf) ///}{p_end}
{phang3}{cmd:prompt("The title provides enough information to identify the topic.") ///}{p_end}
{phang3}{cmd:backend(openrouter) allowcloud workers(4) rundir("runs/title_check")}{p_end}

{pstd}Resume an interrupted run:{p_end}
{phang2}{cmd:. decider resume, rundir("runs/title_check") timeout(120) replace}{p_end}


{marker results}{...}
{title:Stored results}

{pstd}
{cmd:decider} stores the following in {cmd:r()}:

{synoptset 22 tabbed}{...}
{p2col 5 22 26 2: Scalars}{p_end}
{synopt:{cmd:r(N)}}observations in the marked sample{p_end}
{synopt:{cmd:r(N_scored)}}observations scored by the model{p_end}
{synopt:{cmd:r(N_missing)}}observations skipped due to missing/blank text{p_end}
{synopt:{cmd:r(N_flagged)}}observations flagged for human review{p_end}
{synopt:{cmd:r(share_flagged)}}proportion of sample flagged for review{p_end}
{synopt:{cmd:r(mean_top_p)}}mean highest probability ({cmd:choice}/{cmd:score}){p_end}
{synopt:{cmd:r(mean_margin)}}mean probability margin ({cmd:choice}/{cmd:score}){p_end}
{synopt:{cmd:r(mean_prob)}}mean probability ({cmd:noul}){p_end}
{synopt:{cmd:r(N_unstable)}}rows whose label changed under reversed order ({opt reversecheck}){p_end}
{synopt:{cmd:r(share_stable)}}proportion of scored rows order-stable ({opt reversecheck}){p_end}
{synopt:{cmd:r(min_prob)}, {cmd:r(min_margin)}}review thresholds ({cmd:choice}/{cmd:score}){p_end}
{synopt:{cmd:r(threshold)}}review threshold ({cmd:noul}){p_end}
{synopt:{cmd:r(workers)}}worker concurrency used{p_end}
{synopt:{cmd:r(n_requests)}}total successful model/API calls{p_end}
{synopt:{cmd:r(cost_usd)}}reported API cost in USD (when reported){p_end}

{p2col 5 22 26 2: Macros}{p_end}
{synopt:{cmd:r(cmd)}}{cmd:decider}{p_end}
{synopt:{cmd:r(subcmd)}}{cmd:score} or {cmd:resume}{p_end}
{synopt:{cmd:r(varlist)}}input text variable(s){p_end}
{synopt:{cmd:r(type)}}{cmd:choice}, {cmd:score}, or {cmd:noul}{p_end}
{synopt:{cmd:r(prefix)}}variable prefix stub{p_end}
{synopt:{cmd:r(backend)}}backend identifier{p_end}
{synopt:{cmd:r(model)}}requested model identifier{p_end}
{synopt:{cmd:r(returned_model)}}model identifier returned by provider{p_end}
{synopt:{cmd:r(prompt_sha256)}}SHA-256 hash of exact frozen prompt text{p_end}
{synopt:{cmd:r(prompt_file)}}prompt file path (if {opt promptfile()} used){p_end}
{synopt:{cmd:r(run_dir)}}run directory path{p_end}
{synopt:{cmd:r(created_vars)}}list of generated variables{p_end}

{p2col 5 22 26 2: Matrices}{p_end}
{synopt:{cmd:r(dist)}}option distribution matrix ({cmd:N}, {cmd:Share}, {cmd:Mean_Prob}) for {cmd:choice}/{cmd:score}{p_end}
{p2colreset}{...}
