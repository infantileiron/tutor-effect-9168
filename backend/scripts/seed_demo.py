import random,json
from datetime import datetime,timezone
from sqlalchemy import select,func
from src.config.database import Base,engine,SessionLocal
from src.models.entities import User,Student,Plan,Report,Exam,Question,AnswerSheet,Batch
from src.utils.security import hash_password
from src.services.statistics import welch

QUESTIONS=[('1',10,'A'),('2',10,'B'),('3',10,'C'),('4',15,'D'),('5',15,'A'),('6',20,'B'),('7',20,'C')]
OPTIONS=('A','B','C','D')

def seed_grading(db,rng):
    title='2026秋季 · 客观题单元测验'
    exam=db.scalar(select(Exam).where(Exam.title==title))
    if exam: return exam
    exam=Exam(title=title,max_score=100.0,created_at=datetime.now(timezone.utc).isoformat());db.add(exam);db.flush()
    for number,full,answer in QUESTIONS:
        db.add(Question(exam_id=exam.id,number=number,full_score=full,answer=answer,cancelled=False))
    for i in range(48):
        code=f'ZX-2026-{i+1:03}'
        answers={}
        for number,full,answer in QUESTIONS:
            roll=rng.random()
            answers[number]=answer if roll<.62 else rng.choice([o for o in OPTIONS if o!=answer])
        db.add(AnswerSheet(exam_id=exam.id,student_code=code,answers=answers,absent=i%24==0 and i>0,appeal_version=0))
    db.add(Batch(exam_id=exam.id,status='pending',version=0,created_at=datetime.now(timezone.utc).isoformat()))
    db.commit()
    return exam

def seed():
    Base.metadata.create_all(engine)
    rng=random.Random(20261115)
    with SessionLocal() as db:
        for role in ('admin','analyst','viewer'):
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)): db.add(User(email=email,role=role,password_hash=hash_password('Demo123!')))
        for i in range(160):
            code=f'ZX-2026-{i+1:03}'
            group='control' if i<80 else 'intervention'
            score=round(min(100,max(0,rng.gauss(72 if group=='control' else 78,8))),1)
            absent=i%23==0
            if not db.scalar(select(Student).where(Student.code==code)):
                db.add(Student(code=code,name=f'学员{i+1:03}',campus=['城东','城西','滨江','高新'][i%4],group=group,score=None if absent else score,absent=absent))
        title='2026秋季 · 晚间小组辅导'
        plan=db.scalar(select(Plan).where(Plan.title==title))
        if not plan:
            plan=Plan(title=title,campus='全部',exclude_absent=True,description='评估每周两次小组辅导与常规自习的期末成绩差异。缺考学员排除；观察性结果不直接证明因果。');db.add(plan);db.flush()
        db.commit()
        if not db.scalar(select(Report).where(Report.plan_id==plan.id)):
            result=welch(db.scalars(select(Student).order_by(Student.code)).all(),plan)
            db.add(Report(plan_id=plan.id,status='completed',result=result,created_at=datetime.now(timezone.utc).isoformat(),conclusion='干预组均分较高。建议保留教学点分层观察，并在下一轮随机分组验证。',published=True));db.commit()
        seed_grading(db,rng)
        print(json.dumps({table:db.scalar(select(func.count()).select_from(model)) for table,model in [('users',User),('students',Student),('plans',Plan),('reports',Report),('exams',Exam),('sheets',AnswerSheet)]}))
if __name__=='__main__': seed()
