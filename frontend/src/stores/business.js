import{getStudents}from'../api/students.js';import{getPlans}from'../api/plans.js';import{getReports}from'../api/reports.js';import{getExams,getWorkspace}from'../api/grading.js';
export const store={students:[],plans:[],reports:[],user:null,grading:null};
export async function refresh(){[store.students,store.plans,store.reports]=await Promise.all([getStudents(),getPlans(),getReports()]);}
export async function loadGrading(examId){
 const exams=await getExams();
 const currentExam=examId||store.grading?.exam?.id||exams[0]?.id;
 if(!currentExam){store.grading={exams,exam:null,questions:[],sheets:[],batch:null,verification:null};return;}
 const workspace=await getWorkspace(currentExam);
 store.grading={exams,exam:workspace.exam,questions:workspace.questions,sheets:workspace.sheets,batch:workspace.batch,verification:workspace.latest_verification};
}
