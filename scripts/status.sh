#!/usr/bin/env bash
echo "== status =="; curl -s localhost:8080/exam/status
echo; echo "== recent events =="; curl -s localhost:8080/exam/events | tail -60
