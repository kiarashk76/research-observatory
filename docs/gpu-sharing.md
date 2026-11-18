# Shared GPU jobs

GPU jobs remain exclusive unless they explicitly request `--gpu-mode shared` and a positive `--gpu-memory-mib` reservation per GPU. For example:

```bash
python "$LAB_ROOT/lab.py" job start --name "Small-model sweep" --hours 1 --gpus 1 --gpu-mode shared --gpu-memory-mib 4096 --cpu-threads 4 -- python experiment.py
```

Measure peak memory, including CUDA context, optimizer state, compilation and temporary buffers. These are scheduler admission reservations, not hard CUDA memory caps. Overestimated reservations reduce concurrency; underestimated ones can cause out-of-memory failures for sharing jobs. Shared mode does not guarantee a compute fraction or an execution speed. Use exclusive access for timing measurements and unknown/large memory peaks.

`resources.gpu_sharing` in config.yaml enables sharing with at most four jobs per GPU. The scheduler leaves the larger of 1024 MiB or 10% of total memory unused for admission, checks both reserved capacity and currently free memory, and never mixes exclusive jobs or unmanaged GPU processes with shared jobs. Only identity-checked descendants of managed jobs qualify as managed processes. GPU probe failure blocks GPU admission.

An assignment's `max_gpus` counts distinct physical GPUs. Multiple shared jobs may use the same GPU within that grant, but ordinary CPU grants, outstanding-job counts, total compute-hour allowances and the global job limit still apply. Set appropriate explicit CPU counts when submitting small jobs. Reservations are recorded in the durable job record and run manifest and survive daemon restarts. Existing exclusive jobs retain their allocations.

Overview GPU cards show running job names, requested reservations and remaining shared admission capacity. Experiment cards and details show sharing mode and the per-GPU memory reservation. Telemetry is sampled and may differ from capacity at launch.

No MPS service or hardware partitioning is enabled. Benchmark useful completed runs per hour before choosing between independent shared jobs and packed execution in one process. Sharing may help small workloads but can also add contention and per-process memory overhead.

The current server permits 24 concurrent experiment jobs; engineering assignments default to 16 outstanding jobs. Assignment CPU grants may total up to 40 threads, while each individual job remains limited to 32. These are separate config values: budgets.max_jobs, budgets.task_jobs, resources.max_cpu_threads_per_assignment, and resources.max_cpu_threads_per_job. Real CPU availability, GPU memory, and exclusive reservations still govern admission.
