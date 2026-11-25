*! End-to-end offline verification test for decider.ado using backend(mock)
version 16.0
clear all
set more off

* Make the repository root first on adopath so decider.ado and _decider_score.py resolve here
adopath ++ "."

* ---- 0. Test environment diagnostic subcommand (`decider check`) ----
decider check
assert r(cmd) == "decider"
assert r(subcmd) == "check"
assert inlist(r(remote_ready), 0, 1)
assert inlist(r(local_ready), 0, 1)
assert strlen("`r(python_version)'") > 0

* ---- 1. Build a small synthetic dataset including strL, quotes, missing text, and if-subset ----
set obs 5
gen int id = _n
gen strL goal = ""
gen strL session = ""

replace goal    = "Improve Algebra I problem-solving"          in 1
replace session = `"Math Lab | "Hands-On" Algebra Strategies"' in 1

replace goal    = "Strengthen early literacy phonics"          in 2
replace session = "Reading Workshop | Decoding Fluency"        in 2

replace goal    = ""                                           in 3
replace session = "General Wellness Seminar"                   in 3

replace goal    = "Support multilingual learners [UNSTABLE]"   in 4
replace session = "ELD Institute | Academic Vocabulary"        in 4

replace goal    = "Excluded by if-clause"                      in 5
replace session = "Should not be scored"                       in 5

* ---- 2. Test choice + reversecheck + if-clause + multi-field + r() results ----
tempfile pr_file
tempname fh
file open `fh' using `"`pr_file'"', write text replace
file write `fh' "Evaluate whether the professional learning session aligns to the goal." _n
file close `fh'

decider goal session if id <= 4, ///
    type(choice) prefix(al) ///
    promptfile(`"`pr_file'"') ///
    options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///
    backend(mock) workers(2) reversecheck

return list

* Verify r() scalars, macros, and matrix
assert r(cmd) == "decider"
assert r(subcmd) == "score"
assert r(type) == "choice"
assert r(prefix) == "al"
assert r(backend) == "mock"
assert r(N) == 4
assert r(N_scored) == 3
assert r(N_missing) == 1
assert r(workers) == 2
assert r(n_requests) == 6
assert strlen(r(prompt_sha256)) == 64
assert r(mean_top_p) > 0 & r(mean_top_p) <= 1
assert r(mean_margin) >= 0 & r(mean_margin) <= 1
assert r(share_stable) >= 0 & r(share_stable) <= 1

matrix D = r(dist)
assert rowsof(D) == 3
assert colsof(D) == 3
assert D[1,1] + D[2,1] + D[3,1] == 3

* Verify row-level invariants
assert al_status == 1 in 1
assert al_status == 2 in 3
assert al_review == 1 in 3
assert strpos(al_review_reason[3], "Missing goal") > 0
assert missing(al_status) in 5
assert missing(al_class) in 5
assert abs((al_p1[1] + al_p2[1] + al_p3[1]) - 1) < 1e-4

* Verify provenance characteristics
local ch_sha : char al_class[decider_prompt_sha256]
assert "`ch_sha'" == "`r(prompt_sha256)'"

* ---- 3. Test ordered score type (expected index in [1, 3]) ----
decider session if id <= 4, ///
    type(score) prefix(sc) ///
    prompt("Rate the specificity of this session title.") ///
    options("Low" "Medium" "High") ///
    backend(mock)

assert r(type) == "score"
assert r(N_scored) == 4
assert sc_expected[1] >= 1 & sc_expected[1] <= 3
assert missing(sc_expected[5])

* ---- 4. Test noul (yes/no probability) type ----
decider session if id <= 4, ///
    type(noul) prefix(inf) ///
    prompt("The session title identifies a specific instructional topic.") ///
    backend(mock) threshold(0.75)

assert r(type) == "noul"
assert r(threshold) == 0.75
assert r(mean_prob) > 0 & r(mean_prob) < 1
assert inf_prob[1] > 0 & inf_prob[1] < 1
assert missing(inf_prob[5])

* ---- 5. Test privacy & guardrail rejections ----
capture noisily decider session, type(noul) prefix(bad1) prompt("Test") backend(openrouter)
assert _rc == 198

capture noisily decider session, type(choice) prefix(bad2) prompt("Test") options("A" "B") backend(neojev) workers(2)
assert _rc == 198

* Refuse to overwrite without replace
capture noisily decider session, type(noul) prefix(inf) prompt("Test") backend(mock)
assert _rc == 110

* ---- 6. Test crash-safe resume from a persistent rundir ----
local resume_dir "`c(tmpdir)'/decider_test_resume_run"
capture shell rm -rf "`resume_dir'"

decider session if id <= 3, ///
    type(choice) prefix(res) ///
    prompt("Classify session.") ///
    options("Content" "Pedagogy") ///
    backend(mock) rundir("`resume_dir'")

assert r(N_scored) == 3

* Resume must reject changed order/text without overwriting existing results.
clonevar saved_class = res_class
clonevar saved_session = session
gsort -id
capture noisily decider resume, rundir("`resume_dir'") replace
assert _rc == 198
assert res_class == saved_class
sort id
replace session = "Edited input" in 1
capture noisily decider resume, rundir("`resume_dir'") replace
assert _rc == 198
assert res_class == saved_class
replace session = saved_session in 1

* Changes outside the original sample are allowed.
replace session = "Edited outside sample" in 5

* Re-run resume (all 3 rows already scored -> 0 new requests, clean attach with replace)
decider resume, rundir("`resume_dir'") replace
assert r(subcmd) == "resume"
assert r(N_scored) == 3
assert r(n_requests) == 0

* Reject a partial checkpoint row while preserving attached results.
tempfile intact_scores
copy "`resume_dir'/scores.csv" "`intact_scores'", replace
tempname damaged
file open `damaged' using "`resume_dir'/scores.csv", write text append
file write `damaged' "4,scored,1,0.8" _n
file close `damaged'
capture noisily decider resume, rundir("`resume_dir'") replace
assert _rc == 198
assert res_class == saved_class
copy "`intact_scores'" "`resume_dir'/scores.csv", replace

* Empty-memory resume retains its documented direct-import behavior.
clear
decider resume, rundir("`resume_dir'")
assert _N == 3
assert r(subcmd) == "resume"
confirm variable row_id class_idx

capture shell rm -rf "`resume_dir'"

di as result _n "ALL DECIDER STATA TESTS PASSED."
cap log close