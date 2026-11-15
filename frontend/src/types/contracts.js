/** @typedef {{id:number,code:string,name:string,campus:string,group:'control'|'intervention',score:number|null,absent:boolean}} Student */
/** @typedef {{id:number,title:string,campus:string,exclude_absent:boolean,description:string}} Plan */
/** @typedef {{id:number,plan_id:number,status:string,result:object|null,conclusion:string,published:boolean}} Report */
export const contractVersion=1;
/** @typedef {{id:number,title:string,max_score:number,created_at:string}} Exam */
/** @typedef {{id:number,number:string,full_score:number,answer:string|null,cancelled:boolean}} Question */
/** @typedef {{student_code:string,name:string|null,campus:string|null,answers:Object,absent:boolean,appeal_score:number|null,appeal_note:string,appeal_version:number}} AnswerSheet */
/** @typedef {{student_code:string,name:string|null,raw_score:number|null,percent_score:number|null,previous_raw:number|null,previous_percent:number|null,delta_raw:number|null,delta_percent:number|null,contributions:Object,explanation:string,conflict:boolean,appeal_version:number,appeal_score:number|null,resolved_appeal_version:number,resolution_decision:string,published_score:number|null,published_percent:number|null,published_source:string,absent:boolean}} BatchScore */
/** @typedef {{id:number,exam_id:number,status:'pending'|'ready'|'published',version:number,published_version:number|null,published_at:string|null,conflicts:string[],scores:BatchScore[],reflection:Object|null,resolutions:Array<Object>}} Batch */
export const gradingContractVersion=1;
