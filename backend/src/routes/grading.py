from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.utils.security import current_user,editor,admin
from src.schemas.business import (
    ExamInput,AnswerSheetInput,RecalcInput,AppealInput,ResolutionInput,PublishInput,
)
from src.services import grading
from src.models.entities import Verification
from src.controllers.business import serialize
router=APIRouter()

@router.get('/exams')
def exams(db=Depends(get_db),user=Depends(current_user)):
    return grading.list_exams(db)

@router.post('/exams',status_code=201)
def create_exam(data:ExamInput,db=Depends(get_db),user=Depends(editor)):
    return serialize(grading.create_exam(db,data))

@router.get('/exams/{id}/workspace')
def exam_workspace(id:int,db=Depends(get_db),user=Depends(current_user)):
    return grading.workspace(db,id)

@router.put('/exams/{id}/sheets/{code}')
def save_sheet(id:int,code:str,data:AnswerSheetInput,db=Depends(get_db),user=Depends(editor)):
    if data.student_code!=code: raise HTTPException(400,'路径学号与答卷学号不一致')
    return grading.serialize_sheet(grading.upsert_sheet(db,id,data))

@router.post('/exams/{id}/rules')
def save_rules(id:int,data:RecalcInput,db=Depends(get_db),user=Depends(editor)):
    return grading.save_rules(db,id,data.questions)

@router.post('/exams/{id}/appeals',status_code=201)
def appeal(id:int,data:AppealInput,db=Depends(get_db),user=Depends(editor)):
    return grading.serialize_sheet(grading.register_appeal(db,id,data,user.email))

@router.post('/exams/{id}/batches',status_code=201)
def batch(id:int,db=Depends(get_db),user=Depends(editor)):
    return grading.serialize_batch(db,grading.get_or_create_batch(db,id))

@router.get('/batches/{id}')
def batch_detail(id:int,db=Depends(get_db),user=Depends(current_user)):
    return grading.serialize_batch(db,grading.get_batch(db,id))

@router.post('/batches/{id}/recalc')
def recalc(id:int,data:RecalcInput,db=Depends(get_db),user=Depends(editor)):
    return grading.serialize_batch(db,grading.recalc(db,id,data.questions,user.email))

@router.post('/batches/{id}/resolutions')
def resolution(id:int,data:ResolutionInput,db=Depends(get_db),user=Depends(editor)):
    grading.resolve_conflict(db,id,data,user.email)
    return grading.serialize_batch(db,grading.get_batch(db,id))

@router.post('/batches/{id}/publish')
def publish(id:int,data:PublishInput,db=Depends(get_db),user=Depends(admin)):
    batch,replayed=grading.publish(db,id,data.request_key,data.expected_version,user.email)
    payload=grading.serialize_batch(db,batch)
    payload['replayed']=replayed
    return payload

@router.post('/batches/{id}/verify',status_code=201)
def verify(id:int,db=Depends(get_db),user=Depends(editor)):
    return grading.serialize_verification(grading.verify(db,id,user.email))

@router.get('/batches/{id}/verifications')
def verifications(id:int,db=Depends(get_db),user=Depends(current_user)):
    items=db.scalars(select(Verification).where(Verification.batch_id==id).order_by(Verification.id.desc())).all()
    return [grading.serialize_verification(i) for i in items]

@router.get('/verifications/{id}/export')
def export_verification(id:int,db=Depends(get_db),user=Depends(current_user)):
    import json
    from fastapi.responses import Response
    item=db.get(Verification,id)
    if not item: raise HTTPException(404,'检验结果不存在')
    body=grading.serialize_verification(item)
    return Response(json.dumps(body,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="grading-verification-{id}.json"'})
