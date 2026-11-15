import{request,post}from'./client.js';

export const getExams=()=>request('/exams');
export const createExam=data=>post('/exams',data);
export const getWorkspace=id=>request(`/exams/${id}/workspace`);
export const saveSheet=(examId,code,data)=>request(`/exams/${examId}/sheets/${encodeURIComponent(code)}`,{method:'PUT',body:JSON.stringify(data)});
export const registerAppeal=(examId,data)=>post(`/exams/${examId}/appeals`,data);
export const ensureBatch=examId=>post(`/exams/${examId}/batches`,{});
export const saveRules=(examId,questions)=>post(`/exams/${examId}/rules`,{questions});
export const recalcBatch=(batchId,questions)=>post(`/batches/${batchId}/recalc`,{questions});
export const resolveConflict=(batchId,data)=>post(`/batches/${batchId}/resolutions`,data);
export const publishBatch=(batchId,data)=>post(`/batches/${batchId}/publish`,data);
export const verifyBatch=batchId=>post(`/batches/${batchId}/verify`,{});
export const getVerifications=batchId=>request(`/batches/${batchId}/verifications`);
