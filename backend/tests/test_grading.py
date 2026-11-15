"""成绩勘误与整批评分: 验收测试.

覆盖: 取消全部题目、申诉插入、发布中断、逐人裁决、裁决期间改分、
并发发布、发布重试幂等、检验只读完整发布批次、既有报告不变。
"""
import os

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from src.models.entities import (
    Base, AnswerSheet, Batch, BatchScore, Exam, Question, Report, Verification,
)
from src.schemas.business import (
    AppealInput, PublishInput, QuestionInput, RecalcInput, ResolutionInput,
    AnswerSheetInput,
)
from src.services import grading


@pytest.fixture()
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        yield session


QUESTIONS = [
    ('1', 10, 'A'),
    ('2', 10, 'B'),
    ('3', 10, 'C'),
    ('4', 15, 'D'),
    ('5', 15, 'A'),
    ('6', 20, 'B'),
    ('7', 20, 'C'),
]


def make_exam(db, max_score=100.0):
    exam = Exam(title=f'测验-{id(db)}', max_score=max_score, created_at=grading.now())
    db.add(exam)
    db.flush()
    for number, full, answer in QUESTIONS:
        db.add(Question(exam_id=exam.id, number=number, full_score=full, answer=answer,
                        cancelled=False))
    sheets = {
        'S001': {'1': 'A', '2': 'B', '3': 'C', '4': 'D', '5': 'A', '6': 'B', '7': 'C'},  # 100
        'S002': {'1': 'A', '2': 'B', '3': 'D', '4': 'D', '5': 'A', '6': 'B', '7': 'C'},  # 90
        'S003': {'1': 'A', '2': 'C', '3': 'C', '4': 'D', '5': 'A', '6': 'B', '7': 'A'},  # 80
        'S004': {'1': 'A', '2': 'B', '3': 'C', '4': 'D', '5': 'B', '6': 'B', '7': 'C'},  # 85
    }
    for code, answers in sheets.items():
        db.add(AnswerSheet(exam_id=exam.id, student_code=code, answers=answers,
                           absent=False, appeal_version=0))
    db.add(AnswerSheet(exam_id=exam.id, student_code='S005', answers={}, absent=True,
                       appeal_version=0))
    db.commit()
    return exam


def rules_from_exam(db, exam_id, overrides=None):
    overrides = overrides or {}
    output = []
    for question in db.scalars(select(Question).where(Question.exam_id == exam_id)
                               .order_by(Question.id)).all():
        data = {'number': question.number, 'full_score': question.full_score,
                'answer': question.answer, 'cancelled': question.cancelled}
        data.update(overrides.get(question.number, {}))
        output.append(QuestionInput(**data))
    return output


def batch_for(db, exam):
    return grading.get_or_create_batch(db, exam.id)


def test_recalc_pending_to_ready_and_percent(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    assert batch.status == 'pending'
    with pytest.raises(HTTPException) as exc:
        grading.publish(db, batch.id, 'req-000001', 0, 'admin')
    assert exc.value.status_code == 409
    graded = grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    assert graded.status == 'ready' and graded.version == 1
    scores = {s.student_code: s for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    assert scores['S001'].raw_score == 100 and scores['S001'].percent_score == 100
    assert scores['S002'].percent_score == 90
    assert scores['S005'].absent and scores['S005'].percent_score is None
    assert '缺考' in scores['S005'].explanation


def test_missing_answer_scores_zero(db):
    exam = make_exam(db)
    sheet = db.scalar(select(AnswerSheet).where(AnswerSheet.student_code == 'S002',
                                                AnswerSheet.exam_id == exam.id))
    sheet.answers = {'1': 'A'}
    db.commit()
    batch = batch_for(db, exam)
    graded = grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == graded.id,
                                               BatchScore.student_code == 'S002'))
    assert score.raw_score == 10
    assert score.percent_score == 10
    statuses = {c['number']: c['status'] for c in score.contributions['questions']}
    assert statuses['2'] == 'blank'


def test_correct_answer_recalc_explains_delta(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    # 第 3 题标准答案由 C 更正为 D: S002(答 D) 从错误变正确。
    corrected = rules_from_exam(db, exam.id, {'3': {'answer': 'D'}})
    graded = grading.recalc(db, batch.id, corrected, 'analyst')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == graded.id,
                                               BatchScore.student_code == 'S002'))
    # 原始分 90 + 10 = 100；剩余满分仍为 100，折算 100。
    assert score.raw_score == 100 and score.percent_score == 100
    assert score.contributions['delta_raw'] == 10
    assert score.contributions['delta_percent'] == 10
    changes = {c['number']: c for c in score.contributions['changes']}
    assert changes['3']['from'] == 'wrong' and changes['3']['to'] == 'correct'
    assert '第 3 题' in score.explanation and '错误 → 正确' in score.explanation
    assert '90' in score.explanation and '100' in score.explanation


def test_cancel_question_renormalizes_remaining_full(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    # 取消第 7 题(20 分)，剩余满分 80；S003 原本答错第 7 题，取消后 70/80 折算 87.5。
    graded = grading.recalc(
        db, batch.id, rules_from_exam(db, exam.id, {'7': {'cancelled': True}}), 'analyst')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == graded.id,
                                               BatchScore.student_code == 'S003'))
    assert score.raw_score == 70 and score.percent_score == 87.5
    assert score.contributions['remaining_full'] == 80
    assert score.contributions['removed_full'] == 20
    assert score.contributions['delta_percent'] == 17.5
    # 已答对第 7 题的学员在取消后折算分被重新缩放。
    top = db.scalar(select(BatchScore).where(BatchScore.batch_id == graded.id,
                                             BatchScore.student_code == 'S001'))
    assert top.raw_score == 80 and top.percent_score == 100
    assert '第 7 题' in top.explanation and '已取消' in top.explanation
    reflection = graded.__dict__  # noqa: F841
    with pytest.raises(HTTPException):
        # 全部取消无法折算，且不落库。
        grading.recalc(db, batch.id,
                       [QuestionInput(number=q.number, full_score=q.full_score,
                                      answer=q.answer, cancelled=True)
                        for q in rules_from_exam(db, exam.id)], 'analyst')
    current = db.get(Batch, graded.id)
    assert current.version == 2  # 失败的重评没有推进版本


def test_publish_is_single_switch_and_verification_reads_published(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    with pytest.raises(HTTPException):
        grading.verify(db, batch.id, 'analyst')  # 未发布不能检验
    published, replayed = grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    assert not replayed and published.status == 'published'
    assert published.published_version == 1
    scores = {s.student_code: s for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    assert scores['S001'].published_percent == 100
    assert scores['S005'].published_percent is None
    verification = grading.verify(db, batch.id, 'analyst')
    assert verification.statistics['count'] == 4
    assert verification.statistics['absent'] == 1
    evidence_codes = [s['student_code'] for s in verification.evidence['scores']]
    assert 'S001' in evidence_codes and 'S005' in evidence_codes
    assert verification.evidence['rules']
    assert verification.evidence['scores'][0]['contributions']


def test_publish_retry_same_key_does_not_double_apply(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    first, _ = grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    first_checksum, first_time = first.published_checksum, first.published_at
    second, replayed = grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    assert replayed is True
    assert second.published_checksum == first_checksum
    assert second.published_at == first_time


def test_concurrent_publish_does_not_overwrite_newer_version(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    # 另一次发布尝试(不同幂等键)不会覆盖已发布版本。
    again, replayed = grading.publish(db, batch.id, 'req-000002', 1, 'admin')
    assert replayed is True and again.published_version == 1
    # 新重评产生工作版本 2；陈旧的发布请求被拒绝，不覆盖。
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'3': {'answer': 'D'}}), 'analyst')
    with pytest.raises(HTTPException) as exc:
        grading.publish(db, batch.id, 'req-000003', 1, 'admin')
    assert exc.value.status_code == 409
    current = db.get(Batch, batch.id)
    assert current.published_version == 1 and current.version == 2


def test_interrupted_publish_keeps_previous_version(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    before = {s.student_code: s.published_percent for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'3': {'answer': 'D'}}), 'analyst')

    def fail(_session):
        raise RuntimeError('模拟发布中断')

    grading.before_publish_commit = fail
    try:
        with pytest.raises(RuntimeError):
            grading.publish(db, batch.id, 'req-000002', 2, 'admin')
    finally:
        grading.before_publish_commit = None
    db.rollback()
    after = db.get(Batch, batch.id)
    assert after.published_version == 1
    assert after.status == 'ready'
    after_scores = {s.student_code: s.published_percent for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    assert after_scores == before  # 旧版整批保留
    # 中断后可重试同一发布；成功后整批切换。
    published, replayed = grading.publish(db, batch.id, 'req-000003', 2, 'admin')
    assert not replayed and published.published_version == 2
    verification = grading.verify(db, batch.id, 'analyst')
    assert verification.published_version == 2


def test_appeal_conflict_is_isolated_and_resolved_per_student(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S002', score=95, note='复核加分'),
                            'analyst')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == batch.id,
                                               BatchScore.student_code == 'S002'))
    assert score.conflict is True
    current = db.get(Batch, batch.id)
    with pytest.raises(HTTPException) as exc:
        grading.publish(db, batch.id, 'req-000001', current.version, 'admin')
    assert exc.value.status_code == 409 and 'S002' in str(exc.value.detail)
    # 只有被申诉的学员被隔离，其余人仍没有冲突。
    others = db.scalars(select(BatchScore).where(BatchScore.batch_id == batch.id,
                                                 BatchScore.student_code != 'S002')).all()
    assert all(not s.conflict for s in others)
    # 保留申诉分并发布。
    grading.resolve_conflict(db, batch.id,
                             ResolutionInput(student_code='S002', decision='keep_appeal',
                                             expected_appeal_version=1), 'admin')
    published, _ = grading.publish(db, batch.id, 'req-000002', current.version, 'admin')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == published.id,
                                               BatchScore.student_code == 'S002'))
    assert score.published_percent == 95 and score.published_source == 'appeal'
    verification = grading.verify(db, batch.id, 'analyst')
    s002 = [s for s in verification.evidence['scores'] if s['student_code'] == 'S002'][0]
    assert s002['percent_score'] == 95 and s002['source'] == 'appeal'
    assert s002['appeal_version'] == 1


def test_use_recalc_resolution_overrides_appeal(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S002', score=95, note='申诉'), 'analyst')
    grading.resolve_conflict(db, batch.id,
                             ResolutionInput(student_code='S002', decision='use_recalc',
                                             expected_appeal_version=1), 'admin')
    published, _ = grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == published.id,
                                               BatchScore.student_code == 'S002'))
    assert score.published_percent == 90 and score.published_source == 'recalculated'


def test_appeal_during_resolution_is_detected(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S002', score=95, note='第一次申诉'),
                            'analyst')
    # 管理员看到第一版申诉，准备裁决；期间分析员再次改分。
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S002', score=97, note='补充申诉'),
                            'analyst')
    with pytest.raises(HTTPException) as exc:
        grading.resolve_conflict(db, batch.id,
                                 ResolutionInput(student_code='S002', decision='keep_appeal',
                                                 expected_appeal_version=1), 'admin')
    assert exc.value.status_code == 409 and '再次修改' in str(exc.value.detail)
    # 使用最新版本才能裁决。
    grading.resolve_conflict(db, batch.id,
                             ResolutionInput(student_code='S002', decision='keep_appeal',
                                             expected_appeal_version=2), 'admin')
    published, _ = grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == published.id,
                                               BatchScore.student_code == 'S002'))
    assert score.published_percent == 97


def test_appeal_inserted_after_recalc_before_publish_is_detected(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    # 重评后、发布前插入申诉。
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S003', score=88, note='发布前申诉'),
                            'analyst')
    with pytest.raises(HTTPException) as exc:
        grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    assert exc.value.status_code == 409 and 'S003' in str(exc.value.detail)
    batch_row = db.get(Batch, batch.id)
    assert batch_row.status == 'ready'
    assert all(s.published_percent is None for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all())


def test_existing_report_and_previous_verification_are_immutable(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    first_verification = grading.verify(db, batch.id, 'analyst')
    snapshot = dict(first_verification.evidence)

    # 历史报告(旧模块)不受勘误影响。
    report = Report(plan_id=1, status='completed', result={'checksum': 'abc', 'control_n': 80},
                    conclusion='旧结论', published=True, created_at=grading.now())
    db.add(report)
    db.commit()

    # 勘误 + 重评 + 再次发布新版本。申诉到来后先重评形成工作版本 2。
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S004', score=90, note='申诉'),
                            'analyst')
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'7': {'cancelled': True}}),
                   'analyst')
    grading.resolve_conflict(db, batch.id,
                             ResolutionInput(student_code='S004', decision='keep_appeal',
                                             expected_appeal_version=1), 'admin')
    grading.publish(db, batch.id, 'req-000002', 2, 'admin')
    second_verification = grading.verify(db, batch.id, 'analyst')
    assert second_verification.published_version == 2
    assert second_verification.evidence['reflection']['version'] == 2

    db.refresh(report)
    assert report.result == {'checksum': 'abc', 'control_n': 80}
    stale = db.get(Verification, first_verification.id)
    assert stale.evidence == snapshot
    assert stale.published_version == 1


def test_verification_rejects_tampered_or_partial_publish(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-000001', 1, 'admin')
    # 模拟发布后混入未发布日期成绩：快照外的明细不应影响检验，但快照被篡改必须失败。
    tampered = db.get(Batch, batch.id)
    snapshot = dict(tampered.published_snapshot)
    snapshot['scores'] = snapshot['scores'][:-1]
    tampered.published_snapshot = snapshot
    db.commit()
    with pytest.raises(HTTPException) as exc:
        grading.verify(db, batch.id, 'analyst')
    assert exc.value.status_code == 409


def test_parallel_publish_sessions_do_not_overwrite(db):
    """两个独立会话并发送达时，只有一个版本能成为发布版本."""
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    published, _ = grading.publish(db, batch.id, 'req-parallel-001', 1, 'admin')
    v1_checksum = published.published_checksum
    v1_scores = {s.student_code: s.published_percent for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    # 不同幂等键的并发/重复发布请求被视为同一版本已发布，直接返回。
    again, replayed = grading.publish(db, batch.id, 'req-parallel-002', 1, 'admin')
    assert replayed and again.published_checksum == v1_checksum
    assert again.published_version == 1
    # 已发布版本不应被再次写入明细（重试不重复改分）。
    after = {s.student_code: s.published_percent for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    assert after == v1_scores


def test_publish_attempts_are_per_key(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-key-aaaa1', 1, 'admin')
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'1': {'answer': 'D'}}), 'analyst')
    published, replayed = grading.publish(db, batch.id, 'req-key-bbbb2', 2, 'admin')
    assert not replayed and published.published_version == 2
    # 版本 1 的旧请求不能冒充版本 2 的成功。
    with pytest.raises(HTTPException) as exc:
        grading.publish(db, batch.id, 'req-key-aaaa1', 2, 'admin')
    assert exc.value.status_code == 409


def test_recalc_uses_saved_rules_when_payload_empty(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.save_rules(db, exam.id, rules_from_exam(db, exam.id, {'4': {'answer': 'A'}}))
    graded = grading.recalc(db, batch.id, [], 'analyst')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == graded.id,
                                               BatchScore.student_code == 'S004'))
    # S004 答 4:D，标准答案改为 A 后该题错误；原始分 85-15=70。
    assert score.raw_score == 70


def test_save_rules_rejects_duplicate_and_all_cancelled(db):
    exam = make_exam(db)
    with pytest.raises(HTTPException):
        grading.save_rules(db, exam.id, [
            QuestionInput(number='1', full_score=10, answer='A'),
            QuestionInput(number='1', full_score=10, answer='B'),
        ])
    with pytest.raises(HTTPException):
        grading.save_rules(db, exam.id, [
            QuestionInput(number='1', full_score=10, answer='A', cancelled=True),
        ])


def test_failure_after_snapshot_write_keeps_published_batch_consistent(db):
    """发布事务内写明细后中断：旧版保持，工作版本仍可重试."""
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-first-001', 1, 'admin')
    old_snapshot = dict(db.get(Batch, batch.id).published_snapshot)
    old_scores = {s.student_code: (s.published_score, s.published_percent, s.published_source)
                  for s in db.scalars(select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}

    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'2': {'cancelled': True}}),
                   'analyst')

    def explode(_session):
        raise RuntimeError('killed before commit')

    grading.before_publish_commit = explode
    try:
        with pytest.raises(RuntimeError):
            grading.publish(db, batch.id, 'req-second-002', 2, 'admin')
    finally:
        grading.before_publish_commit = None
    db.rollback()

    current = db.get(Batch, batch.id)
    assert current.published_version == 1
    assert current.published_snapshot == old_snapshot
    assert current.status == 'ready'
    rows = {s.student_code: (s.published_score, s.published_percent, s.published_source)
            for s in db.scalars(select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    assert rows == old_scores
    # 中断的发布没有留下 done 的幂等记录，可用同一 key 重试成功。
    published, replayed = grading.publish(db, batch.id, 'req-second-002', 2, 'admin')
    assert not replayed and published.published_version == 2


def test_verify_after_new_working_version_uses_last_published_snapshot(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.publish(db, batch.id, 'req-v1-00001', 1, 'admin')
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id, {'1': {'cancelled': True}}),
                   'analyst')
    current = db.get(Batch, batch.id)
    assert current.status == 'ready' and current.published_version == 1
    verification = grading.verify(db, batch.id, 'analyst')
    assert verification.published_version == 1
    assert verification.statistics['count'] == 4
    scores = {s['student_code']: s for s in verification.evidence['scores']}
    assert scores['S001']['percent_score'] == 100


def test_conflict_rejection_leaves_no_pending_publish_key(db):
    from src.models.entities import PublishAttempt
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    grading.register_appeal(db, exam.id,
                            AppealInput(student_code='S002', score=95, note='申诉'), 'analyst')
    with pytest.raises(HTTPException):
        grading.publish(db, batch.id, 'req-conflict-01', 1, 'admin')
    attempts = db.scalars(select(PublishAttempt).where(
        PublishAttempt.batch_id == batch.id,
        PublishAttempt.request_key == 'req-conflict-01')).all()
    assert not attempts
    grading.resolve_conflict(db, batch.id,
                             ResolutionInput(student_code='S002', decision='keep_appeal',
                                             expected_appeal_version=1), 'admin')
    published, replayed = grading.publish(db, batch.id, 'req-conflict-01', 1, 'admin')
    assert not replayed and published.published_version == 1


def test_absent_student_cannot_receive_appeal_score(db):
    exam = make_exam(db)
    batch = batch_for(db, exam)
    grading.recalc(db, batch.id, rules_from_exam(db, exam.id), 'analyst')
    with pytest.raises(HTTPException) as exc:
        grading.register_appeal(db, exam.id,
                                AppealInput(student_code='S005', score=70, note='错登'),
                                'analyst')
    assert exc.value.status_code == 409
    published, _ = grading.publish(db, batch.id, 'req-absent-001', 1, 'admin')
    score = db.scalar(select(BatchScore).where(BatchScore.batch_id == batch.id,
                                               BatchScore.student_code == 'S005'))
    assert score.absent and score.published_percent is None
