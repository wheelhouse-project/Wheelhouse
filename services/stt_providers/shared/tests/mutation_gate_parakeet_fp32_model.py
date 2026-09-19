"""Prove the full-precision model delivery keeps its two load-bearing checks.

Run from the shared service directory:

    uv run python tests/mutation_gate_parakeet_fp32_model.py
    uv run python tests/mutation_gate_parakeet_fp32_model.py --check

wh-parakeet-fp32-shipped-model, criterion A5. The shipped speech model
changed on 2026-09-07 from one int8 archive to five loose full-precision
files fetched from a pinned Hugging Face commit. That change removed two
properties the old design had for free, and every mutation here breaks one
of the checks that replaced them.

**The archive verified itself as a whole.** One SHA-256 covered every byte,
so a truncated or altered file could not reach the model directory. With
five separate downloads there is no such digest, and nothing stands between
a damaged file and a promoted model except a per-file check: one
Invoke-VerifiedDownload call per table entry, each carrying that entry's
own SHA-256 and a URL built from the pinned commit. The first group of
mutations walks the table with one shared digest, downloads only the first
file, and drops the file name from the URL.

**The archive was all-or-nothing.** Extraction either produced the whole
tree or produced nothing. Five downloads can end with four files on disk,
so the assembly builds a per-run working tree, judges THAT tree for
completeness, and promotes only a tree that passed. The second group
assembles straight into the final path, ignores the assembly's verdict,
accepts a short copy, and swallows the name of a file it could not copy.

**The external weights are the new completeness requirement.** The
full-precision encoder is split: encoder.onnx is a 41 MB graph and
encoder.weights is a 2.32 GiB payload that onnxruntime resolves beside it.
A tree without encoder.weights has every filename the int8 build needed, so
the old completeness check called it complete, promoted it, and left a
model that fails inside onnxruntime with nothing to repair. One mutation
removes that requirement.

**The residue sweep has to know the new file names.** It swept one
"<dir>.tar.bz2*" glob; it now sweeps one glob per table entry. A sweep
still looking for the archive strands 2.5 GB of downloads on every machine
forever, so one mutation puts the archive glob back.

**And it has to keep knowing the OLD ones.** Every glob is built from
$ModelDirName, so renaming the model also ends the sweep of everything the
previous release left behind: the old names carry "-int8" where the new
ones stop at "v3". An upgrading machine would keep the 650 MB archive, its
per-run download working files, and a crashed run's extraction tree
forever. Two mutations aim the two $PreviousModelDirName globs at the new
name, which the globs above them already cover.

The provider's coded default is the last mutation. It is the last-resort
model location on a machine with no override file, and the installer's
$ModelDirName is checked against it, so a wrong name here sends that
machine to a directory that will never exist.

Every mutation but the last edits a PowerShell file. The runner parses a
.ps1 mutant before running it, which matters more here than usual: the
installer's test harness begins by parsing the whole script, so one mutant
that does not parse would fail every selected test at once and read as
caught.

Node ids, not whole files: test_installer.py holds over 170 tests and takes
about eleven minutes, while the thirty-one named below cover model
delivery and run in about a minute. Every name is validated against real
collection before the first mutation runs.
"""
import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path, but this keeps it working when the gate
# is invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PARAKEET = SHARED.parent / "sherpa_offline_parakeet_stt_server"
MAIN = PARAKEET / "main.py"
OVERRIDE_TESTS = "tests/test_model_path_override.py"

# The delivery half lives in the release tooling: the installer is a
# PowerShell script, and its tests drive it through a PowerShell subprocess
# from the scripts/release service.
RELEASE = SHARED.parents[2] / "scripts" / "release"
INSTALLER = RELEASE / "public" / "install-wheelhouse.ps1"

_INSTALLER_FILE = "tests/test_installer.py"
INSTALLER_TESTS = tuple(
    f"{_INSTALLER_FILE}::{name}"
    for name in (
        "test_installer_parses_clean",
        "test_the_pinned_model_source_is_the_full_precision_hugging_face_commit",
        "test_the_installer_and_the_provider_agree_on_the_model_directory_name",
        "test_every_model_file_is_downloaded_with_its_own_digest",
        "test_a_missing_model_file_leaves_nothing_at_the_final_model_path",
        "test_full_precision_completeness_requires_the_external_weights",
        "test_a_partial_model_tree_is_reinstalled_not_reported_installed",
        "test_putting_the_model_in_place_runs_no_external_program",
        "test_copy_model_files_builds_the_tree_from_the_downloads",
        "test_copy_model_files_names_the_file_it_could_not_copy",
        "test_copy_model_files_rejects_a_short_copy",
        "test_model_assembly_failure_message_includes_the_step_output",
        "test_a_failed_model_assembly_stops_the_install_and_says_why",
        "test_a_junction_in_model_residue_does_not_delete_its_target",
        "test_an_unreadable_directory_probe_does_not_fail_the_model_sweep",
        "test_already_installed_path_cleans_extraction_residue",
        "test_cleanup_failure_after_promotion_is_not_a_false_failure",
        "test_the_residue_sweep_removes_the_previous_releases_leftovers",
        "test_the_previous_releases_model_is_kept_until_the_new_one_is_complete",
        "test_a_failure_to_remove_the_previous_model_is_not_a_false_failure",
        "test_a_junction_at_the_previous_model_does_not_delete_its_target",
        "test_an_empty_previous_model_name_removes_nothing",
        "test_override_file_is_bomless_and_parses",
        "test_the_override_file_names_no_model_that_was_never_downloaded",
        "test_a_synced_parakeet_without_a_model_is_turned_off",
        "test_a_synced_parakeet_with_a_model_is_left_alone",
        "test_an_unsynced_parakeet_is_left_to_the_sync_loop",
        "test_a_partial_model_tree_counts_as_no_model_for_the_alternative",
        "test_the_install_turns_off_a_model_less_parakeet_before_the_override",
        "test_the_previous_model_dirname_names_the_int8_build",
        "test_entry_cleanup_does_not_delete_a_model_promoted_after_the_check",
    )
)


MUTATIONS = [
    # ---- integrity: every file carries its own digest -------------------
    {
        # The design error the per-file check exists to prevent, in one
        # line: walk the table but verify each file against the first
        # entry's digest. Four of the five files then verify nothing, and
        # the one whose digest is right is the 41 MB graph, not the 2.32
        # GiB payload beside it.
        "name": "one-shared-digest-for-every-file",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "            -ExpectedSha256 $file.Sha256 `\n",
        "new": "            -ExpectedSha256 $ModelFiles[0].Sha256 `\n",
        "expect": ["test_every_model_file_is_downloaded_with_its_own_digest"],
    },
    {
        # Only the first file is fetched. The rest never arrive, so on a
        # clean machine the assembly fails; on a machine that still holds
        # a dead run's downloads it succeeds with whatever those were.
        "name": "only-the-first-model-file-is-downloaded",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    foreach ($file in $ModelFiles) {\n        $fileNumber++\n",
        "new": "    foreach ($file in @($ModelFiles[0])) {\n        $fileNumber++\n",
        "expect": ["test_every_model_file_is_downloaded_with_its_own_digest"],
    },
    {
        # The pinned-commit URL loses the file name, so all five requests
        # ask for the same thing and no file is fetched from the commit
        # the digests were measured at.
        "name": "the-url-drops-the-file-name",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        Invoke-VerifiedDownload -Url "$ModelUrl/$($file.Name)" `\n',
        "new": '        Invoke-VerifiedDownload -Url "$ModelUrl" `\n',
        "expect": [
            "test_every_model_file_is_downloaded_with_its_own_digest",
            "test_a_missing_model_file_leaves_nothing_at_the_final_model_path",
        ],
    },
    # ---- completeness: the external weights are required ----------------
    {
        # The defect this change fixed. Without encoder.weights the tree
        # has every filename the int8 build needed, so the check calls it
        # complete, the installer promotes it, and the provider fails
        # inside onnxruntime with nothing left on disk to repair.
        "name": "external-weights-not-required-for-completeness",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        # The requirement goes, not the block around it: an emptied `if`
        # body still parses in PowerShell, but a mutation that leaves a
        # statement behind cannot be mistaken for a syntax accident.
        "old": '                $required += "encoder.weights"\n',
        "new": "                $required += @()\n",
        "expect": [
            "test_full_precision_completeness_requires_the_external_weights",
        ],
    },
    # ---- all-or-nothing: nothing partial reaches the final path ---------
    {
        # Assemble straight into the path the entry gate reads on the next
        # run. A killed or short copy then leaves a partial tree exactly
        # where a later run looks for a finished one.
        "name": "the-model-is-assembled-at-the-final-path",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    $stagedDir = Join-Path $stagingRoot $ModelDirName\n",
        "new": "    $stagedDir = $extractedDir\n",
        "expect": [
            "test_a_missing_model_file_leaves_nothing_at_the_final_model_path",
        ],
    },
    {
        # The assembly's verdict is ignored, so an incomplete staged tree
        # is promoted. This is the same end state as the mutation above,
        # reached through the other half of the guard.
        "name": "the-assembly-verdict-is-ignored",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if ($extract.ExitCode -ne 0 -or -not (Test-ModelComplete -ModelDir $stagedDir)) {\n",
        "new": "    if ($false) {\n",
        # test_model_assembly_failure_message_includes_the_step_output is
        # NOT a catcher here, and the first full sweep reported this
        # mutation as a survivor for naming it. That test reads the
        # installer's source with a regular expression and asserts the
        # Stop-Install message interpolates $(...Output...)
        # (test_installer.py:1363-1380). This mutation changes the `if`
        # condition and leaves the message text untouched, so a
        # source-text test cannot catch it, by construction. The
        # behavioural tests below do. With the branch disabled the promotion
        # runs anyway: an incomplete staged tree reaches the final path, and
        # a staged tree that was never built at all makes the rename throw,
        # so the user is told "Placing the assembled speech model failed"
        # with a .NET path message instead of the assembly step's own words.
        "expect": [
            "test_a_missing_model_file_leaves_nothing_at_the_final_model_path",
            "test_a_failed_model_assembly_stops_the_install_and_says_why",
        ],
    },
    {
        # A file that arrives short without an exception passes the
        # completeness check, which only asks for a non-empty regular
        # file, and produces a model that fails at load.
        "name": "a-short-copy-is-accepted",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "        if ($null -eq $copiedLength -or $copiedLength -ne $sourceLength) {\n",
        "new": "        if ($false) {\n",
        "expect": ["test_copy_model_files_rejects_a_short_copy"],
    },
    {
        # The copy failure is still a failure, but the message no longer
        # names the file. The step this replaced reported 'tar exit code
        # 1' and left the setup log with no diagnosis at all, which is the
        # outcome this line exists to avoid.
        "name": "a-copy-failure-does-not-name-its-file",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '            $problems += "$($file.Name): $($_.Exception.Message)"\n            continue\n',
        "new": '            $problems += "a file could not be copied"\n            continue\n',
        "expect": ["test_copy_model_files_names_the_file_it_could_not_copy"],
    },
    # ---- delivery: the sweep knows what it is sweeping ------------------
    {
        # The sweep still looks for the archive. Every machine then keeps
        # 2.5 GB of downloads forever, and the already-installed path,
        # which exists to clear exactly that, clears nothing.
        "name": "the-residue-sweep-still-looks-for-the-archive",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '            foreach ($item in @(Get-ChildItem -LiteralPath $DownloadsDir -Filter "$ModelDirName.$($file.Name)*" -ErrorAction SilentlyContinue)) {\n',
        "new": '            foreach ($item in @(Get-ChildItem -LiteralPath $DownloadsDir -Filter "$ModelDirName.tar.bz2*" -ErrorAction SilentlyContinue)) {\n',
        "expect": [
            "test_a_junction_in_model_residue_does_not_delete_its_target",
            "test_an_unreadable_directory_probe_does_not_fail_the_model_sweep",
            "test_already_installed_path_cleans_extraction_residue",
            "test_cleanup_failure_after_promotion_is_not_a_false_failure",
        ],
    },
    # ---- the sweep still knows the PREVIOUS release's names --------------
    {
        # Renaming the model ends the sweep of everything the old name left
        # behind, because every other glob here is built from
        # $ModelDirName. This mutation aims the models-directory legacy
        # glob at the new name, which the glob above it already covers, so
        # an upgrading machine keeps a crashed run's extraction tree --
        # up to 650 MB -- forever.
        "name": "the-previous-releases-working-trees-are-stranded",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        foreach ($item in @(Get-ChildItem -LiteralPath $ModelsDir -Filter "$PreviousModelDirName.extracting*" -ErrorAction SilentlyContinue)) {\n',
        "new": '        foreach ($item in @(Get-ChildItem -LiteralPath $ModelsDir -Filter "$ModelDirName.extracting*" -ErrorAction SilentlyContinue)) {\n',
        "expect": [
            "test_the_residue_sweep_removes_the_previous_releases_leftovers",
        ],
    },
    {
        # The downloads half of the same defect. The previous release's
        # archive and the .partial-<pid> working files a killed download
        # left beside it are stranded, including on the machines the old
        # cleanup told "it will be removed on the next run".
        "name": "the-previous-releases-downloads-are-stranded",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        foreach ($item in @(Get-ChildItem -LiteralPath $DownloadsDir -Filter "$PreviousModelDirName.tar.bz2*" -ErrorAction SilentlyContinue)) {\n',
        "new": '        foreach ($item in @(Get-ChildItem -LiteralPath $DownloadsDir -Filter "$ModelDirName.tar.bz2*" -ErrorAction SilentlyContinue)) {\n',
        "expect": [
            "test_the_residue_sweep_removes_the_previous_releases_leftovers",
        ],
    },
    {
        # QUESTIONS item 96's ordering guarantee, removed. The previous
        # release's installed model goes ONLY after the new model is
        # verified in place. Without the check, a run that reaches the
        # sweep with an incomplete new model deletes the only usable model
        # on the machine -- and an interrupted upgrade is exactly the case
        # where the old model is still what the user is speaking to.
        "name": "the-old-model-is-swept-without-checking-the-new-one",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "            if ([System.IO.Directory]::Exists($previousInstalled) -and\n                    (Test-ModelComplete -ModelDir (Join-Path $ModelsDir $ModelDirName))) {\n",
        "new": "            if ([System.IO.Directory]::Exists($previousInstalled)) {\n",
        "expect": [
            "test_the_previous_releases_model_is_kept_until_the_new_one_is_complete",
        ],
    },
    {
        # The removal itself, gone. An upgraded machine then keeps 640 MB
        # of int8 weights nothing reads, which is the whole of item 96.
        # The status line is deliberately left in place by this mutation:
        # a test that believed the message instead of reading the disk
        # would stay green, and the catcher below reads the disk.
        "name": "the-previous-releases-installed-model-is-left-behind",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "                    Remove-Item -LiteralPath $previousInstalled -Recurse -Force -ErrorAction Stop\n",
        "new": "                    $null = $previousInstalled\n",
        "expect": [
            "test_the_residue_sweep_removes_the_previous_releases_leftovers",
        ],
    },
    {
        # The emptiness guard, removed. Join-Path with an empty child
        # path returns the PARENT, so the removal below then aims
        # Remove-Item -Recurse at the whole models directory and deletes
        # every model on the machine, the one just installed included.
        # The shipped constant is never empty, but a caller that loads
        # these functions without the script's top-level constants
        # reaches exactly this state -- the test harness does, which is
        # how the defect was found.
        "name": "an-empty-previous-model-name-sweeps-the-models-directory",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "        if (-not [string]::IsNullOrWhiteSpace($PreviousModelDirName)) {\n",
        "new": "        if ($true) {\n",
        "expect": [
            "test_an_empty_previous_model_name_removes_nothing",
        ],
    },
    {
        # Codex round 3 finding wh-parakeet-fp32-shipped-model.1.4. The
        # recovery instruction goes back to Write-Status, which writes a
        # [+] host line and nothing else. The graphical wizard runs this
        # script hidden (SW_HIDE) and only a NOTICE line reaches its
        # finish page, so a wizard user loses Parakeet from the engine
        # menu and is never told that rerunning setup and choosing
        # Parakeet installs it. The engine is still disabled correctly,
        # so every check except the tagged-output one still passes -- this
        # mutation is what proves the NOTICE assertion is load-bearing.
        "name": "the-recovery-instruction-never-reaches-the-wizard",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '    Write-InstallNotice "Parakeet has no speech model on this computer, so it is turned off. Run the installer again and choose Parakeet to install its model."\n',
        "new": '    Write-Status "Parakeet has no speech model on this computer, so it is turned off. Run the installer again and choose Parakeet to install its model."\n',
        "expect": [
            "test_a_synced_parakeet_without_a_model_is_turned_off",
        ],
    },
    {
        # QUESTIONS item 103: the disable itself, gone. A Google Cloud
        # or Distil-Whisper install then keeps offering Parakeet as a
        # switchable engine with no model behind it, which is the
        # startup failure ("Model directory not found") the ruling
        # exists to end.
        "name": "the-alternative-parakeet-is-left-enabled-without-a-model",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    Set-TomlProviderDisabled -ConfigPath $ConfigPath\n",
        "new": "    $null = $ConfigPath\n",
        "expect": [
            "test_a_synced_parakeet_without_a_model_is_turned_off",
        ],
    },
    {
        # The disable stops asking whether a model is there and turns
        # Parakeet off on every install that synced it -- including the
        # machines that HAVE the model. Turning off a working
        # alternative is the worse defect of the two.
        "name": "a-parakeet-with-a-model-is-turned-off-anyway",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if (Test-ModelComplete -ModelDir (Join-Path $ModelsDir $ModelDirName)) { return }\n",
        "new": "    if ($false) { return }\n",
        "expect": [
            "test_a_synced_parakeet_with_a_model_is_left_alone",
        ],
    },
    {
        # Folder-exists in place of the completeness rule. An
        # interrupted earlier run leaves a tree holding tokens.txt and
        # no ONNX files; Test-Path accepts it, Parakeet stays on the
        # menu, and onnxruntime rejects it at load with nothing the
        # user can repair.
        "name": "the-model-check-becomes-a-folder-check",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if (Test-ModelComplete -ModelDir (Join-Path $ModelsDir $ModelDirName)) { return }\n",
        "new": "    if (Test-Path -LiteralPath (Join-Path $ModelsDir $ModelDirName)) { return }\n",
        "expect": [
            "test_a_partial_model_tree_counts_as_no_model_for_the_alternative",
        ],
    },
    {
        # The guard that leaves an UNSYNCED Parakeet to the
        # unsynced-provider loop, gone. The config is then rewritten by
        # a step that has no reason to touch it, and the user is told
        # the model is missing when the real problem is the
        # environment.
        "name": "an-unsynced-parakeet-is-disabled-here-too",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if ($SyncedProviders -notcontains \"parakeet_tdt\") { return }\n",
        "new": "    if ($false) { return }\n",
        "expect": [
            "test_an_unsynced_parakeet_is_left_to_the_sync_loop",
        ],
    },
    {
        # The override file names the model unconditionally again. An
        # install that downloaded no model then writes a path that run
        # never created, and main.py reads that entry ahead of the
        # tracked config and the coded default.
        "name": "the-override-names-a-model-that-was-never-downloaded",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if (Test-ModelComplete -ModelDir (Join-Path $ModelsDir $ModelDirName)) {\n",
        "new": "    if ($true) {\n",
        "expect": [
            "test_the_override_file_names_no_model_that_was_never_downloaded",
        ],
    },
    {
        # The mirror of the mutation above: the section is never
        # written, so a correct Parakeet install leaves the provider to
        # resolve its own default and the installer's chosen models
        # directory is silently ignored.
        "name": "the-override-never-names-the-model-it-installed",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    if (Test-ModelComplete -ModelDir (Join-Path $ModelsDir $ModelDirName)) {\n",
        "new": "    if ($false) {\n",
        "expect": [
            "test_override_file_is_bomless_and_parses",
        ],
    },
    {
        # The wiring, gone: Invoke-MainInstall no longer calls the
        # disable. Nothing the harness can execute changes, because the
        # harness cannot run Invoke-MainInstall -- which is why the
        # catcher is a source-structure test, and why this mutation is
        # here to prove that test can still fail.
        "name": "the-install-never-calls-the-disable",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "    Disable-ParakeetWithoutModel -ConfigPath (Join-Path $AppDir (Join-Path $providerDirs[\"parakeet_tdt\"] \"config.toml\")) -SyncedProviders $syncedProviders\n",
        "new": "    $null = $syncedProviders\n",
        "expect": [
            "test_the_install_turns_off_a_model_less_parakeet_before_the_override",
        ],
    },
    {
        # The concurrent-installer data-loss defect, restored. The entry
        # gate observed 'incomplete' and deletes the final path on the
        # strength of that snapshot, so a complete model another run
        # promoted in between is destroyed. Moving the tree aside and
        # re-verifying the exact tree that moved is the whole protection.
        # wh-parakeet-fp32-shipped-model.1.3: the test that names this
        # race shadowed Test-ModelComplete, which the entry gate never
        # calls, so it returned at the already-installed gate and this
        # mutation would have survived it.
        "name": "the-entry-gate-deletes-the-final-path-in-place",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": "            [System.IO.Directory]::Move($extractedDir, $staleDir)\n",
        "new": "            Remove-Item -LiteralPath $extractedDir -Recurse -Force\n",
        "expect": [
            "test_entry_cleanup_does_not_delete_a_model_promoted_after_the_check",
        ],
    },
    # ---- the coded default names the shipped model ----------------------
    {
        # The suffix comes back. A machine with no override file then
        # looks for the int8 directory the installer no longer creates,
        # and the provider reports a missing model on a correct install.
        "name": "the-coded-default-names-the-int8-build",
        "service": PARAKEET,
        "test_file": OVERRIDE_TESTS,
        "file": MAIN,
        "old": 'DEFAULT_MODEL_DIRNAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"\n',
        "new": 'DEFAULT_MODEL_DIRNAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"\n',
        "expect": [
            "test_the_default_dirname_is_the_full_precision_model",
            "test_the_default_dirname_does_not_name_a_quantized_build",
        ],
    },
]


if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS))
