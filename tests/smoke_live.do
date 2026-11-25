*! Optional live smoke test for decider.ado (synthetic records only).
*! Toggle `run_neojev` and `run_openrouter` below before running.
*! OpenRouter incurs small API charges (6 calls across 3 non-missing rows).
version 16.0
clear all
set more off

adopath ++ "."

* ---- Configuration switches --------------------------------------------------
local run_neojev     = 1   // Set to 1 on an Apple Silicon Mac with NeoJev set up
local run_openrouter = 1   // Set to 1 if OPENROUTER_API_KEY is set and cloud calls are OK

* Optional overrides if not using `decider setup, local` (~/.decider/):
local neojev_python    ""
local neojev_vendordir ""

* ---- Synthetic 4-row dataset (1 missing text row + 3 complete rows) ----------
set obs 4
gen int id = _n
gen strL goal = ""
gen strL session = ""

replace goal    = "Improve Algebra I problem-solving"        in 1
replace session = "Math Lab | Hands-On Algebra Strategies"   in 1

replace goal    = "Strengthen early literacy phonics"        in 2
replace session = "Reading Workshop | Decoding Fluency"      in 2

replace goal    = ""                                         in 3
replace session = "General Wellness Seminar"                 in 3

replace goal    = "Support multilingual learners"            in 4
replace session = "ELD Institute | Academic Vocabulary"      in 4

local prompt_text "Evaluate whether the professional learning session aligns to the goal."

if (`run_neojev' == 1) {
    local py_opt  = cond(`"`neojev_python'"'    != "", `"python("`neojev_python'")"', "")
    local ven_opt = cond(`"`neojev_vendordir'"' != "", `"vendordir("`neojev_vendordir'")"', "")
    decider goal session, ///
        type(choice) prefix(al_loc) ///
        prompt("`prompt_text'") ///
        options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///
        backend(neojev) `py_opt' `ven_opt' reversecheck
    assert r(N_scored) == 3
    assert r(N_missing) == 1
    matlist r(dist)
}

if (`run_openrouter' == 1) {
    decider goal session, ///
        type(choice) prefix(al_or) ///
        prompt("`prompt_text'") ///
        options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///
        backend(openrouter) allowcloud workers(2) reversecheck
    assert r(N_scored) == 3
    assert r(N_missing) == 1
    matlist r(dist)
}

list id al_*_class al_*_top_p al_*_order_stable, abbreviate(20)
di as result _n "LIVE SMOKE TEST COMPLETE."
