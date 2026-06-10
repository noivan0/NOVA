"""
nova.watcher — inotify-based event-driven autonomous loop for NOVA.

Two watchers run as long-lived processes and replace cron jobs:

  BrainWatcher   watches brain.db / kanban.db for changes → triggers learn/synthesize/dream
  KBWatcher      watches kb/ and skills/ directories → triggers kb_pipeline / wiki / index

Start both from a supervisor or manually:

    python -m nova.watcher.brain --nova-home ~/.nova
    python -m nova.watcher.kb   --nova-home ~/.nova
"""
