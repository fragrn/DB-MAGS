# Post4 335k Scale HITL Variant

This run keeps the same sparse low-id token distribution assumption used in the 15s reproduction, but changes `issues` cardinality from 2,500,000 rows to 335,000 rows to match the forum-disclosed scale more closely.

- Database: `incident_repro_post4_335k`
- Issues rows: `335000`
- Token rows: `50,100,150,200,250,300,350,400,450,500`
- High-id rows do not contain `255392`.
- Goal: measure how much latency remains when data scale matches the post while distribution keeps the long reverse-scan mechanism.
