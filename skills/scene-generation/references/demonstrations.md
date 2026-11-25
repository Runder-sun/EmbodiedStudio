# Demonstration collection and experience reuse

Use this guide when the request includes robot demonstrations. Use the native session, frozen environments, independently frozen behaviors, and existing delivery process. Do not introduce another collection scheduler, layout registry, or review process. See the [stage and layout interfaces](api.md#independent-stages-and-layout-sets).

## Resume and reuse existing work

Reconcile existing layouts, behavior revisions, real episodes, and jobs in flight. Use references in the original project and its progress records. Continue outstanding jobs under their existing identities. Count successful demonstrations by actual environment revision, task, behavior identity, and demonstration eligibility. Record interface probes, failures, repeated runs on one layout, and multiple views separately.

Reuse valid assets, robot integration, and same-revision environment and scoring evidence. Freeze behavior-only changes independently with `robot_run(behavior={sourceRef,helperRefs},...)`. For an export-only change, convert, read back, and review the new export. Neither change requires rebuilding the environment or recollecting successful demonstrations. If the environment must change, freeze a new revision. Retain the old records and splits, and obtain evidence for the new revision.

## Develop the expert before scaling up collection

After the first success, save the rerunnable behavior closure and document the applicable robot, coordinate frames, observations, actions, and contact conditions. Choose further layouts from the predefined training split that expose changes in position or relationships. Turn experience into code that computes goals from current observations, checks outcomes, and handles deviations. Prefer one reusable expert or a small number of versions with explicit conditions of use. Avoid baking a different absolute action sequence into every layout. Report exactly what a single success establishes.

Shift from expert development to batch collection according to actual cross-layout results. No fixed trial count or extra pass gate applies. Development episodes that already satisfy the request can count as training data. A later behavior update does not erase an earlier success. Bind each sample to the behavior actually executed. Keep test layouts out of expert debugging, parameter selection, and demonstration augmentation.

## Localize failures efficiently

Read the original job's error, stage, records, and timing breakdown. Distinguish interface and numerical errors from actual motion and contact failures. If recorded inputs reproduce a field, joint-order, coordinate, planning-start, or trajectory-boundary issue, run a small numerical check inside the project before another physical attempt. A numerical pass does not establish robot success. Use the existing wait interface for actual system blockers outside public tools or permissions.

Reuse the planner and verified feedback mappings within an execution. Screen legal candidates against the current measured state. Advance or recover based on observed consequences. If a numerical candidate fails, try another legal candidate at that state. After physical state changes, update the starting state and collision relationships. Do not roll back an executed prefix or replay an old trajectory from a different state. The generator writes phase goals and recovery. Use the shared [trajectory execution interface](api.md#motion-planning-and-policy-reuse).

## Collection and verification

For a batch, reuse the frozen expert on the required layouts. Each layout has its own revision. `repetitions` repeats the same initial state. There is no multi-layout batch call. Independent preparation or export can proceed while waiting. Keep physical operations on the same world in order. Preserve agreed cameras, required records, physical execution, and scoring when optimizing throughput. Offline rerendering and parallel environments require explicit, verified system support.

Read the actual score, execution result, and demonstration eligibility for each proposed episode and submit it to the existing independent internal review. Reuse images and evidence from the same revision. Recheck changed or unexamined areas. Entering a new layout or export stage does not call for repeating all historical work. Review of one layout cannot certify others.

Use the existing progress record to indicate whether you are developing the expert or collecting with it. Record the valid behavior and its scope, successful layouts, the next work item, and references to actual jobs and failures. Use available job timings for startup, execution, and recording. Treat missing times as unknown and avoid adding nested intervals. Report progress through newly qualified demonstrations, success on distinct layouts, and elapsed time. File, build, and tool-call counts do not measure output.
