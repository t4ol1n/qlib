# Trainer Classes and Architecture

<cite>
**Referenced Files in This Document**
- [trainer.py](file://qlib/model/trainer.py)
- [recorder.py](file://qlib/workflow/recorder.py)
- [manage.py](file://qlib/workflow/task/manage.py)
- [base.py](file://qlib/model/base.py)
- [expm.py](file://qlib/workflow/expm.py)
</cite>

## Table of Contents
1. Introduction
2. Project Structure
3. Core Components
4. Architecture Overview
5. Detailed Component Analysis
6. Dependency Analysis
7. Performance Considerations
8. Troubleshooting Guide
9. Conclusion

## Introduction
This document explains QLib’s trainer class hierarchy and architecture for training models across single-process, delayed, and distributed workflows. It covers the base Trainer interface, its core methods (train, end_train, worker), and four concrete implementations:
- TrainerR: simple sequential training using Recorders
- DelayTrainerR: delayed execution where preparation is separated from model fitting
- TrainerRM: task-manager-based parallel training with a shared task pool
- DelayTrainerRM: distributed delayed training that defers heavy work to workers or separate machines

It also documents the trainer lifecycle, state management via status tags, and how trainers coordinate with QLib’s experiment tracking system.

## Project Structure
The trainer subsystem lives under qlib/model and integrates with workflow components for experiment tracking and task management:
- Model layer: base model interfaces and trainer abstractions
- Workflow layer: Recorder and Experiment Manager for logging and artifact storage
- Task layer: TaskManager and run_task for distributed scheduling and lifecycle control

```mermaid
graph TB
subgraph "Model Layer"
TBase["Trainer (base)"]
TR["TrainerR"]
DTR["DelayTrainerR"]
TRM["TrainerRM"]
DTRM["DelayTrainerRM"]
end
subgraph "Workflow Layer"
R["Recorder / MLflowRecorder"]
EM["ExpManager"]
end
subgraph "Task Layer"
TM["TaskManager"]
RT["run_task"]
end
TBase --> TR
TBase --> DTR
TBase --> TRM
TBase --> DTRM
TR --> R
DTR --> R
TRM --> TM
TRM --> RT
DTRM --> TM
DTRM --> RT
R --> EM
```

**Diagram sources**
- [trainer.py:131-207](file://qlib/model/trainer.py#L131-L207)
- [trainer.py:209-339](file://qlib/model/trainer.py#L209-L339)
- [trainer.py:341-620](file://qlib/model/trainer.py#L341-L620)
- [recorder.py:28-494](file://qlib/workflow/recorder.py#L28-L494)
- [manage.py:35-559](file://qlib/workflow/task/manage.py#L35-L559)
- [expm.py:22-200](file://qlib/workflow/expm.py#L22-L200)

**Section sources**
- [trainer.py:131-207](file://qlib/model/trainer.py#L131-L207)
- [recorder.py:28-494](file://qlib/workflow/recorder.py#L28-L494)
- [manage.py:35-559](file://qlib/workflow/task/manage.py#L35-L559)
- [expm.py:22-200](file://qlib/workflow/expm.py#L22-L200)

## Core Components
- Base Trainer interface defines the contract for training orchestration:
  - train: prepare and/or execute training; returns models/recordings
  - end_train: finalize training; may be no-op for immediate trainers
  - worker: optional backend worker for distributed execution
  - has_worker: indicates if worker support exists
  - is_delay: indicates whether real training is deferred to end_train
- Concrete trainers:
  - TrainerR: linear, recorder-based training with status tagging
  - DelayTrainerR: separates preparation (begin) from fitting (end)
  - TrainerRM: uses TaskManager to schedule tasks and supports workers
  - DelayTrainerRM: combines delayed execution with task manager for distributed scenarios

Key integration points:
- Experiment tracking via Recorder (MLflow-backed) for parameters, artifacts, metrics, and tags
- TaskManager for distributed scheduling, persistence, and lifecycle states
- Status tags on recorders to track training progress

**Section sources**
- [trainer.py:131-207](file://qlib/model/trainer.py#L131-L207)
- [trainer.py:209-339](file://qlib/model/trainer.py#L209-L339)
- [trainer.py:341-620](file://qlib/model/trainer.py#L341-L620)
- [recorder.py:28-494](file://qlib/workflow/recorder.py#L28-L494)
- [manage.py:35-559](file://qlib/workflow/task/manage.py#L35-L559)

## Architecture Overview
The trainer architecture follows a two-phase pattern for delayed trainers and a single-phase pattern for immediate trainers. Immediate trainers complete all work in train and mark completion in end_train. Delayed trainers split work into:
- Preparation phase (train): create experiments, log parameters, persist task definitions
- Execution phase (end_train): perform heavy operations like model fitting and record generation

Distributed training leverages TaskManager to queue tasks and run_task to fetch and execute them, enabling multi-process or multi-machine execution.

```mermaid
sequenceDiagram
participant User as "User Code"
participant Trainer as "TrainerR/DelayTrainerR"
participant RM as "TrainerRM/DelayTrainerRM"
participant Rec as "Recorder"
participant TM as "TaskManager"
participant RT as "run_task"
User->>Trainer : call(train(tasks))
alt Immediate (TrainerR)
Trainer->>Rec : start experiment, log params
Trainer->>Rec : fit model, save artifacts
Trainer->>Rec : set tag begin/end
else Delayed (DelayTrainerR)
Trainer->>Rec : start experiment, log params
Trainer-->>User : return recorders (not fitted yet)
User->>Trainer : call(end_train(recs))
Trainer->>Rec : resume, fit model, save artifacts
Trainer->>Rec : set tag end
end
User->>RM : call(train(tasks))
alt Immediate (TrainerRM)
RM->>TM : create tasks (WAITING)
RM->>RT : run_task(WAITING -> DONE)
RM->>Rec : set tag begin/end
else Delayed (DelayTrainerRM)
RM->>TM : create tasks (PART_DONE)
RM-->>User : return recorders (not fitted yet)
User->>RM : call(end_train(recs))
RM->>RT : run_task(PART_DONE -> DONE)
RM->>Rec : set tag end
end
```

**Diagram sources**
- [trainer.py:209-339](file://qlib/model/trainer.py#L209-L339)
- [trainer.py:341-620](file://qlib/model/trainer.py#L341-L620)
- [manage.py:217-559](file://qlib/workflow/task/manage.py#L217-L559)
- [recorder.py:247-494](file://qlib/workflow/recorder.py#L247-L494)

## Detailed Component Analysis

### Base Trainer Interface
- Purpose: define a uniform API for training orchestration and lifecycle control
- Key methods:
  - train: implement per-trainer logic; immediate trainers do full work here
  - end_train: finalize; delayed trainers do heavy work here
  - worker: optional method to start background workers for distributed execution
  - has_worker: capability flag for worker support
  - is_delay: flag indicating deferred execution
- Integration:
  - Uses Recorder to start experiments, log parameters, and manage artifacts
  - Uses TaskManager and run_task for distributed scheduling when applicable

```mermaid
classDiagram
class Trainer {
+bool delay
+train(tasks, *args, **kwargs) list
+end_train(models, *args, **kwargs) list
+is_delay() bool
+has_worker() bool
+worker() void
}
class TrainerR
class DelayTrainerR
class TrainerRM
class DelayTrainerRM
Trainer <|-- TrainerR
Trainer <|-- DelayTrainerR
Trainer <|-- TrainerRM
Trainer <|-- DelayTrainerRM
```

**Diagram sources**
- [trainer.py:131-207](file://qlib/model/trainer.py#L131-L207)

**Section sources**
- [trainer.py:131-207](file://qlib/model/trainer.py#L131-L207)

### TrainerR: Sequential Recorder-Based Training
- Behavior:
  - Iterates over tasks sequentially
  - Starts an experiment per task, logs parameters, fits model, saves artifacts
  - Sets begin and end status tags on each Recorder
- Configuration options:
  - experiment_name: default experiment name
  - train_func: custom training function (default: task_train)
  - call_in_subproc: force memory release by running in subprocess
  - default_rec_name: optional recorder naming
- Use cases:
  - Simple sequential training without distribution needs
  - When you want explicit control over per-task Recorder lifecycle

```mermaid
flowchart TD
Start([Start TrainerR.train]) --> Normalize["Normalize tasks to list"]
Normalize --> Loop{"For each task"}
Loop --> |Yes| Begin["Start experiment<br/>Log params<br/>Fit model<br/>Save artifacts"]
Begin --> TagBegin["Set tag train_status = begin"]
TagBegin --> Next["Next task"]
Next --> Loop
Loop --> |No| EndTrain["Call end_train to set tag end"]
EndTrain --> Return(["Return recorders"])
```

**Diagram sources**
- [trainer.py:209-291](file://qlib/model/trainer.py#L209-L291)

**Section sources**
- [trainer.py:209-291](file://qlib/model/trainer.py#L209-L291)

### DelayTrainerR: Delayed Sequential Training
- Behavior:
  - train performs only preparation (start experiment, log params)
  - end_train resumes the Recorder and executes heavy operations (fitting, recording)
  - Skips already completed tasks based on status tags
- Configuration options:
  - experiment_name: default experiment name
  - train_func: default begin_task_train
  - end_train_func: default end_task_train
- Use cases:
  - Online simulation where signal preparation happens at different times but model fitting can be batched later
  - Decoupling lightweight preparation from expensive fitting

```mermaid
sequenceDiagram
participant U as "User"
participant DTR as "DelayTrainerR"
participant R as "Recorder"
U->>DTR : train(tasks)
DTR->>R : begin_task_train(experiment, log params)
DTR-->>U : return recorders (not fitted)
U->>DTR : end_train(recs)
loop For each recorder
DTR->>R : check tag train_status
alt not end
DTR->>R : end_task_train(resume, fit, save)
DTR->>R : set tag train_status = end
else already end
DTR-->>DTR : skip
end
end
DTR-->>U : return recorders
```

**Diagram sources**
- [trainer.py:293-339](file://qlib/model/trainer.py#L293-L339)
- [trainer.py:74-128](file://qlib/model/trainer.py#L74-L128)

**Section sources**
- [trainer.py:293-339](file://qlib/model/trainer.py#L293-L339)
- [trainer.py:74-128](file://qlib/model/trainer.py#L74-L128)

### TrainerRM: Task Manager-Based Parallel Training
- Behavior:
  - Creates tasks in TaskManager (MongoDB-backed collection)
  - Optionally runs tasks immediately via run_task or delegates to workers
  - Tracks task IDs and sets begin/end tags on recorders
  - Supports waiting for completion when not delayed
- Configuration options:
  - experiment_name: default experiment name
  - task_pool: MongoDB collection name (defaults to experiment_name)
  - train_func: default task_train
  - skip_run_task: if True, only run_task in worker; useful for CPU-to-GPU handoff
  - default_rec_name: optional recorder naming
- Use cases:
  - Multi-process or multi-machine training
  - Robust task lifecycle management with retry and status transitions

```mermaid
sequenceDiagram
participant U as "User"
participant TRM as "TrainerRM"
participant TM as "TaskManager"
participant RT as "run_task"
participant R as "Recorder"
U->>TRM : train(tasks)
TRM->>TM : create_task(tasks) -> _id_list
alt not skip_run_task
TRM->>RT : run_task(WAITING -> DONE)
end
TRM->>TM : wait(query=_id_list) unless delayed
TRM->>R : set tag train_status = begin
TRM->>R : set tag _id in TaskManager
TRM-->>U : return recorders
```

**Diagram sources**
- [trainer.py:341-489](file://qlib/model/trainer.py#L341-L489)
- [manage.py:217-559](file://qlib/workflow/task/manage.py#L217-L559)

**Section sources**
- [trainer.py:341-489](file://qlib/model/trainer.py#L341-L489)
- [manage.py:217-559](file://qlib/workflow/task/manage.py#L217-L559)

### DelayTrainerRM: Distributed Delayed Training
- Behavior:
  - train creates tasks and marks them PART_DONE (preparation done)
  - end_train triggers run_task to finish fitting and sets final tags
  - worker method can run end_train on separate processes/machines
- Configuration options:
  - experiment_name: default experiment name
  - task_pool: MongoDB collection name
  - train_func: default begin_task_train
  - end_train_func: default end_task_train
  - skip_run_task: allows separating preparation and execution across machines
- Use cases:
  - Distributed pipelines where preparation runs on one machine and heavy training on another
  - Batched training after signals are prepared asynchronously

```mermaid
sequenceDiagram
participant U as "User"
participant DTRM as "DelayTrainerRM"
participant TM as "TaskManager"
participant RT as "run_task"
participant R as "Recorder"
U->>DTRM : train(tasks)
DTRM->>TM : create_task(tasks) -> _id_list
DTRM->>RT : run_task(WAITING -> PART_DONE)
DTRM-->>U : return recorders (not fitted)
U->>DTRM : end_train(recs)
DTRM->>RT : run_task(PART_DONE -> DONE)
DTRM->>R : set tag train_status = end
DTRM-->>U : return recorders
```

**Diagram sources**
- [trainer.py:491-620](file://qlib/model/trainer.py#L491-L620)
- [manage.py:485-559](file://qlib/workflow/task/manage.py#L485-L559)

**Section sources**
- [trainer.py:491-620](file://qlib/model/trainer.py#L491-L620)
- [manage.py:485-559](file://qlib/workflow/task/manage.py#L485-L559)

### State Management with Status Tags
- Record-level tags:
  - train_status: begin_task_train vs end_task_train to indicate lifecycle phases
  - _id in TaskManager: links Recorder to TaskManager task for distributed flows
- Task-level statuses (TaskManager):
  - waiting: ready to be executed
  - running: currently being processed
  - part_done: intermediate completion (used by delayed trainers)
  - done: fully completed
- Experiment tracking:
  - Recorder starts/resumes experiments, logs parameters, artifacts, and metrics
  - ExpManager manages active experiments and ensures proper start/end semantics

```mermaid
stateDiagram-v2
[*] --> Waiting : "create_task"
Waiting --> Running : "fetch_task"
Running --> PartDone : "partial completion"
Running --> Done : "complete"
PartDone --> Running : "resume"
PartDone --> Done : "complete"
```

**Diagram sources**
- [manage.py:79-82](file://qlib/workflow/task/manage.py#L79-L82)
- [manage.py:265-383](file://qlib/workflow/task/manage.py#L265-L383)

**Section sources**
- [trainer.py:217-220](file://qlib/model/trainer.py#L217-L220)
- [trainer.py:349-355](file://qlib/model/trainer.py#L349-L355)
- [manage.py:79-82](file://qlib/workflow/task/manage.py#L79-L82)
- [manage.py:265-383](file://qlib/workflow/task/manage.py#L265-L383)

### Coordinator with Experiment Tracking System
- Trainers use Recorder to:
  - Start experiments and optionally resume existing ones
  - Log parameters and environment variables
  - Save model artifacts and datasets
  - Set tags to track training phases
- ExpManager provides context management for experiments and ensures consistent lifecycle handling

```mermaid
sequenceDiagram
participant T as "Trainer"
participant R as "Recorder"
participant E as "ExpManager"
T->>E : start_exp(experiment_name, recorder_name)
E-->>T : active experiment
T->>R : log_params, save_objects
T->>R : set_tags(train_status=begin/end)
T->>E : end_exp(status=FINISHED/FAILED)
```

**Diagram sources**
- [trainer.py:74-128](file://qlib/model/trainer.py#L74-L128)
- [recorder.py:247-494](file://qlib/workflow/recorder.py#L247-L494)
- [expm.py:22-200](file://qlib/workflow/expm.py#L22-L200)

**Section sources**
- [trainer.py:74-128](file://qlib/model/trainer.py#L74-L128)
- [recorder.py:247-494](file://qlib/workflow/recorder.py#L247-L494)
- [expm.py:22-200](file://qlib/workflow/expm.py#L22-L200)

## Dependency Analysis
- Trainer classes depend on:
  - Recorder for experiment logging and artifact storage
  - TaskManager and run_task for distributed scheduling and lifecycle control
  - Model base classes for fit/predict contracts
- Coupling:
  - TrainerR and DelayTrainerR couple tightly with Recorder lifecycle
  - TrainerRM and DelayTrainerRM couple with TaskManager for distributed execution
- External dependencies:
  - MongoDB for TaskManager persistence
  - MLflow for Recorder implementation

```mermaid
graph LR
TrainerR --> Recorder
DelayTrainerR --> Recorder
TrainerRM --> TaskManager
TrainerRM --> run_task
DelayTrainerRM --> TaskManager
DelayTrainerRM --> run_task
Recorder --> MLflow
TaskManager --> MongoDB
```

**Diagram sources**
- [trainer.py:209-620](file://qlib/model/trainer.py#L209-L620)
- [recorder.py:247-494](file://qlib/workflow/recorder.py#L247-L494)
- [manage.py:35-559](file://qlib/workflow/task/manage.py#L35-L559)

**Section sources**
- [trainer.py:209-620](file://qlib/model/trainer.py#L209-L620)
- [recorder.py:247-494](file://qlib/workflow/recorder.py#L247-L494)
- [manage.py:35-559](file://qlib/workflow/task/manage.py#L35-L559)

## Performance Considerations
- Memory usage:
  - TrainerR supports running tasks in subprocesses to force memory release
- Concurrency:
  - TrainerRM and DelayTrainerRM enable parallel execution via TaskManager and run_task
  - Use skip_run_task to decouple preparation and execution across machines
- I/O overhead:
  - Avoid unnecessary artifact writes; Dataset configuration can prevent dumping large data during online inference
- Waiting strategies:
  - TrainerRM waits for tasks to complete unless in delayed mode; ensure workers are running to avoid blocking

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
- Tasks stuck in RUNNING:
  - Check TaskManager status and reset if necessary
  - Ensure run_task is invoked with correct before_status and after_status
- Missing artifacts:
  - Verify Recorder started properly and artifacts saved within experiment context
- Duplicate tasks:
  - TaskManager deduplicates by filter; confirm task definitions are consistent
- Delays in distributed flows:
  - Confirm workers are running and connected to the same task pool
  - Validate query filters match expected task IDs

**Section sources**
- [manage.py:265-383](file://qlib/workflow/task/manage.py#L265-L383)
- [manage.py:458-483](file://qlib/workflow/task/manage.py#L458-L483)
- [recorder.py:335-396](file://qlib/workflow/recorder.py#L335-L396)

## Conclusion
QLib’s trainer hierarchy provides flexible patterns for training workflows:
- Use TrainerR for straightforward sequential training with explicit Recorder lifecycle
- Use DelayTrainerR to decouple preparation from heavy fitting, ideal for online simulation
- Use TrainerRM for robust distributed training with task lifecycle management
- Use DelayTrainerRM for distributed delayed training across machines or processes

Status tags and TaskManager states provide clear visibility into training phases, while Recorder and ExpManager integrate seamlessly with experiment tracking for reproducibility and analysis.

[No sources needed since this section summarizes without analyzing specific files]