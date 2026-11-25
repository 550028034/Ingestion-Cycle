#!/usr/bin/env python3
"""Offline-ready SOP workflow.

The full workflow, prompt, schema, folders, state, catalog upsert and output creation
remain active. The provider adapter intentionally stops at the LLM boundary with
status LLM_REQUIRED. A valid LLM response can later be imported without rerunning
file preparation.
"""
from __future__ import annotations
import argparse, hashlib, json, mimetypes, os, re, shutil, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field
from openpyxl import load_workbook

SUPPORTED={'.pdf','.docx','.doc','.pptx','.ppt','.xlsx','.xls','.png','.jpg','.jpeg','.webp','.tif','.tiff','.bmp','.txt','.md','.html','.htm'}
OFFICE={'.docx','.doc','.pptx','.ppt','.xlsx','.xls'}

class StrictModel(BaseModel): model_config=ConfigDict(extra='forbid')
class Evidence(StrictModel):
    source_locator:str
    exact_excerpt:str
class Keyword(StrictModel):
    keyword:str
    keyword_type:Literal['Error Code / Message','Alert','Status / State','System / Application','Service / Component','API / Method','Tool / Script / File','Database / Collection','Field / Identifier','Account / Role / Group','Ticket Reference','Document Reference','Contact / Escalation','Training Course','Business Object','Action / Workflow']
    evidence:list[Evidence]
class ErrorAlert(StrictModel):
    value:str
    classification:Literal['error_code','error_message','alert']
    evidence:list[Evidence]
class VisualElement(StrictModel):
    visual_id:str; page_or_location:str
    visual_type:Literal['screenshot','diagram','flowchart','table','chart','form','photo','icon','other']
    title_or_caption:Optional[str]=None
    visible_text:list[str]=[]; objects_and_controls:list[str]=[]; spatial_relationships:list[str]=[]
    instructional_meaning:str; example_values:list[str]=[]; mandatory_values:list[str]=[]; warnings:list[str]=[]
    confidence:int=Field(ge=0,le=100)
class Gate(StrictModel):
    gate_id:str; condition:str; evidence_required:str
    on_missing:Literal['NEEDS_INFORMATION','NEEDS_VALIDATION','MANUAL_REVIEW','SOP_NOT_APPLICABLE']
    evidence:list[Evidence]
class ProcedureStep(StrictModel):
    step_id:str; sequence:int=Field(ge=1); action:str
    action_type:Literal['validation','diagnostic','information_request','production_change','scheduled_production_change','notification','closure','escalation']
    target_system:Optional[str]=None; prerequisites:list[str]=[]; required_inputs:list[str]=[]
    expected_result:Optional[str]=None; validation:Optional[str]=None; failure_action:Optional[str]=None
    approval_required:bool; automatable:bool; risk_level:Literal['low','medium','high','critical']; evidence:list[Evidence]
class Section(StrictModel):
    section_id:str; number:Optional[str]=None; title:str; complete_content:list[str]
    tables:list[list[list[str]]]=[]; evidence:list[Evidence]
class Reference(StrictModel):
    document_number:str; title:Optional[str]=None
    relationship:Literal['parent_standard','approved_product_list','template','form','related_sop','supersedes','superseded_by','other']
    required_for_execution:bool; evidence:list[Evidence]
class SOPSemantic(StrictModel):
    document_title:str; document_number:str; revision:str; revision_date:str
    status:Literal['current','superseded','draft','unknown']; document_type:str; functional_area:str
    purpose:str; business_outcome:str; target_teams:list[str]; systems_applications:list[str]; tags:list[str]
    error_codes_alerts:list[ErrorAlert]; keywords:list[Keyword]; summary:str
    included_scenarios:list[str]; excluded_scenarios:list[str]; applicability_gates:list[Gate]
    prerequisites:list[str]; required_inputs:list[str]; risks:list[str]; procedure:list[ProcedureStep]
    validation_requirements:list[str]; escalation_conditions:list[str]; closure_criteria:list[str]
    references:list[Reference]; sections:list[Section]; visuals:list[VisualElement]; security_notes:list[str]
    missing_or_ambiguous_information:list[str]; extraction_confidence:int=Field(ge=0,le=100); requires_human_review:bool

PROMPT='''You are a Senior AI Ticketing SOP Knowledge Engineer. Read the supplied SOP as a complete visual document. Analyze every page, paragraph, header, footer, table, list, image, screenshot, diagram, chart, form, caption, callout, color-coded state, and spatial relationship.

Return one JSON object conforming exactly to the supplied JSON Schema.

STRICT RULES
1. Do not omit information. Preserve complete normative content in sections.complete_content and also populate normalized fields.
2. Use only information visible in the supplied file. Never invent metadata, revisions, dates, steps, tags, systems, errors, or relationships.
3. Every keyword, error/alert, gate, step, reference and section must cite a precise source locator and short exact excerpt.
4. Analyze images directly. Capture visible text, controls, fields, arrows, highlighted regions, row relationships and instructional meaning.
5. Separate example values from mandatory values.
6. Generic uppercase words are not error codes. DOC identifiers are document references; INC/SCTASK identifiers are ticket references.
7. Preserve procedure order, prerequisites, stop conditions, validation, approvals, risk, failure actions, escalation, and closure.
8. Select zero or more tags only from APPROVED_TAGS. Assign a tag only when the SOP content directly supports the tag's operational meaning. Do not invent, normalize, rename, or alter tag spelling. If no approved tag applies, return [].
9. Never reproduce passwords, API keys, tokens, secrets, private keys or connection strings. Add a security note without quoting the value.
10. If critical content cannot be proven, use "Not stated", explain the ambiguity, lower confidence, and require human review.
11. "Revision History" is a heading, not a revision value.
12. Return JSON only.

SOURCE_FILE: {{SOURCE_FILE}}
APPROVED_TAGS: {{APPROVED_TAGS}}
'''

def utc(): return datetime.now(timezone.utc).isoformat()
def sha(b:bytes): return hashlib.sha256(b).hexdigest()
def atomic_json(path:Path,obj:Any):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',delete=False,dir=path.parent,suffix='.tmp') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2); tmp=Path(f.name)
    os.replace(tmp,path)
def load(path:Path,default):
    if not path.exists(): return default
    with path.open(encoding='utf-8-sig') as f:return json.load(f)
def find_soffice():
    xs=[shutil.which('soffice'),shutil.which('libreoffice')]
    if os.name=='nt': xs += [r'C:\Program Files\LibreOffice\program\soffice.exe',r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']
    return next((str(x) for x in xs if x and Path(x).exists()),None)
def prepare_visual_document(source:Path,prepared_dir:Path)->Path:
    prepared_dir.mkdir(parents=True,exist_ok=True)
    if source.suffix.lower() not in OFFICE:
        out=prepared_dir/source.name
        if not out.exists(): shutil.copy2(source,out)
        return out
    soffice=find_soffice()
    if not soffice: raise RuntimeError('LibreOffice required to render Office documents to PDF before LLM visual processing.')
    result=subprocess.run([soffice,'--headless','--convert-to','pdf','--outdir',str(prepared_dir),str(source)],capture_output=True,text=True,timeout=240)
    out=prepared_dir/f'{source.stem}.pdf'
    if result.returncode or not out.exists(): raise RuntimeError(f'LibreOffice conversion failed: {result.stderr or result.stdout}')
    return out
def job_id(source:Path): return f'{source.stem}_{sha(source.read_bytes())[:12]}'
def clean_text(value)->str:
    return str(value or '').strip()

def dedupe_casefold(values:list[str])->list[str]:
    result=[]; seen=set()
    for value in values:
        text=clean_text(value)
        key=text.casefold()
        if text and key not in seen:
            seen.add(key); result.append(text)
    return result

def load_tag_registry(workbook_path:Path)->dict:
    """Load approved tags plus authoritative SOP mappings from SOP_Metadata_All.xlsx."""
    empty={'approved_tags':[],'tags_by_sop_id':{},'tags_by_source_file':{}}
    if not workbook_path.exists():
        return empty
    wb=load_workbook(workbook_path,read_only=True,data_only=True)
    if 'Tags' not in wb.sheetnames:
        wb.close(); raise RuntimeError("Metadata workbook must contain a 'Tags' worksheet.")
    approved=[]; by_sop={}
    ws=wb['Tags']
    for tag_value, count_value, sop_ids_value in ws.iter_rows(min_row=2,max_col=3,values_only=True):
        tag=clean_text(tag_value)
        if not tag: continue
        approved.append(tag)
        ids_text=clean_text(sop_ids_value)
        if not ids_text or ids_text in {'—','-'}: continue
        for sop_id in [x.strip().upper() for x in re.split(r'[,;\n]+',ids_text) if x.strip()]:
            by_sop.setdefault(sop_id,[]).append(tag)
    by_source={}
    if 'SOP Metadata' in wb.sheetnames:
        meta=wb['SOP Metadata']
        headers=[clean_text(c.value) for c in next(meta.iter_rows(min_row=1,max_row=1))]
        index={name:i for i,name in enumerate(headers)}
        for row in meta.iter_rows(min_row=2,values_only=True):
            sop_id=clean_text(row[index['SOP ID']]).upper() if 'SOP ID' in index else ''
            source=clean_text(row[index['Source File Name']]) if 'Source File Name' in index else ''
            if source:
                by_source[source.casefold()]=dedupe_casefold(by_sop.get(sop_id,[]))
    wb.close()
    return {
        'approved_tags':dedupe_casefold(approved),
        'tags_by_sop_id':{k:dedupe_casefold(v) for k,v in by_sop.items()},
        'tags_by_source_file':by_source
    }

def validate_selected_tags(selected:list[str],approved:list[str])->list[str]:
    """Return exact canonical spellings and reject any LLM-invented tag."""
    canonical={tag.casefold():tag for tag in approved}
    invalid=[tag for tag in selected if clean_text(tag).casefold() not in canonical]
    if invalid:
        raise RuntimeError('LLM returned unapproved tags: '+', '.join(invalid))
    return dedupe_casefold([canonical[clean_text(tag).casefold()] for tag in selected])

def resolve_sop_tags(sid:str,source_name:str,llm_tags:list[str],registry:dict)->tuple[list[str],str]:
    """Workbook mapping wins; otherwise keep schema-valid LLM selections."""
    mapped=registry['tags_by_sop_id'].get(sid.upper(),[])
    if not mapped:
        mapped=registry['tags_by_source_file'].get(source_name.casefold(),[])
    if mapped:
        return mapped,'metadata_workbook'
    return validate_selected_tags(llm_tags,registry['approved_tags']),'llm_from_approved_list'

def create_request(source:Path,prepared:Path,tags:list[str],job_dir:Path)->dict:
    prompt=PROMPT.replace('{{SOURCE_FILE}}',source.name).replace('{{APPROVED_TAGS}}',json.dumps(tags,ensure_ascii=False))
    schema=SOPSemantic.model_json_schema()
    (job_dir/'prompt.txt').write_text(prompt,encoding='utf-8')
    atomic_json(job_dir/'response_schema.json',schema)
    req={'job_id':job_dir.name,'status':'LLM_REQUIRED','created_at_utc':utc(),
         'source_file':str(source.resolve()),'prepared_file':str(prepared.resolve()),
         'mime_type':mimetypes.guess_type(prepared.name)[0] or 'application/octet-stream',
         'prompt_file':str((job_dir/'prompt.txt').resolve()),'schema_file':str((job_dir/'response_schema.json').resolve()),
         'expected_response_file':str((job_dir/'llm_response.json').resolve()),
         'llm_call':{'required':True,'performed':False,'provider':None,'message':'LLM required: submit prepared_file with prompt.txt and enforce response_schema.json.'}}
    atomic_json(job_dir/'request.json',req); return req
def invoke_llm(prepared_file:Path,prompt:str,schema:dict)->Optional[dict]:
    """Call the configured LLM provider and return its JSON response."""
    # TODO: Call the selected provider SDK with the document, prompt, and schema.
    # Return the provider's parsed JSON object; do not write pipeline outputs here.
    return None

def call_llm_placeholder(request:dict)->dict:
    """Dispatch prepared job inputs to the provider adapter, if implemented."""
    response=invoke_llm(
        Path(request['prepared_file']),
        Path(request['prompt_file']).read_text(encoding='utf-8'),
        load(Path(request['schema_file']),{})
    )
    if response is None:
        return {'status':'LLM_REQUIRED','job_id':request['job_id'],'message':'LLM required. No provider call is configured. Use request.json, prompt.txt, response_schema.json and prepared_file, then save the model output as llm_response.json and run --resume.'}
    atomic_json(Path(request['expected_response_file']),response)
    return {'status':'LLM_RESPONSE_READY','job_id':request['job_id'],'message':'LLM response saved. Run --resume to validate it and complete the workflow.'}
def next_sop_id(catalog:list[dict],semantic:SOPSemantic,filename:str):
    for x in catalog:
        if semantic.document_number!='Not stated' and x.get('document_number')==semantic.document_number:return x['sop_id']
        if x.get('source_file_name')==filename:return x['sop_id']
    nums=[]
    for x in catalog:
        m=re.fullmatch(r'SOP-(\d+)',str(x.get('sop_id','')))
        if m: nums.append(int(m.group(1)))
    return f'SOP-{max(nums+[0])+1:03d}'
def materialize(job_dir:Path,root:Path,args)->dict:
    req=load(job_dir/'request.json',None)
    if not req: raise RuntimeError(f'Missing request.json in {job_dir}')
    response=job_dir/'llm_response.json'
    if not response.exists(): return {'job_id':job_dir.name,'status':'LLM_REQUIRED','message':'Place valid model JSON in llm_response.json.'}
    semantic=SOPSemantic.model_validate_json(response.read_text(encoding='utf-8'))
    source=Path(req['source_file']); catalog_path=root/args.catalog; catalog=load(catalog_path,[])
    sid=next_sop_id(catalog,semantic,source.name)
    registry=load_tag_registry(root/args.metadata_workbook)
    resolved_tags,tag_source=resolve_sop_tags(sid,source.name,semantic.tags,registry)
    semantic.tags=resolved_tags
    sem=semantic.model_dump(); sem['sop_id']=sid; sem['tag_assignment_source']=tag_source
    detail={'schema_name':'JSON_SOP_Content','schema_version':'4.0.0-offline-llm','generated_at_utc':utc(),
            'source_integrity':{'file_name':source.name,'source_path':str(source.resolve()),'mime_type':mimetypes.guess_type(source.name)[0] or 'application/octet-stream'},
            'semantic_content':sem,'document_structure':{'sections':sem['sections'],'visuals':sem['visuals']},
            'processing':{'status':'complete','provider':'external_llm','job_id':job_dir.name,'requires_human_review':semantic.requires_human_review,'confidence':semantic.extraction_confidence,'tag_assignment_source':tag_source}}
    filt={'semantic_content':sem,'relevant_body_blocks':sem['sections'],'relevant_visuals':sem['visuals'],'source_integrity':detail['source_integrity']}
    stem=f'{sid}_{source.stem}'.replace(' ','_')
    atomic_json(root/args.content_dir/f'{stem}_SOP_CONTENT.json',detail)
    atomic_json(root/args.filter_dir/f'{stem}_SOP_FILTER_CONTENT.json',filt)
    rec={'sop_id':sid,'document_title':semantic.document_title,'source_file_name':source.name,'source_path':str(source.resolve()),
         'document_number':semantic.document_number,'revision':semantic.revision,'revision_date':semantic.revision_date,
         'document_type':semantic.document_type,'functional_area':semantic.functional_area,'systems_applications':semantic.systems_applications,
         'error_codes_alerts':[x.value for x in semantic.error_codes_alerts],'summary':semantic.summary,'tags':semantic.tags,
         'keywords':[{'keyword':x.keyword,'keyword_type':x.keyword_type} for x in semantic.keywords],
         'notes':'; '.join(semantic.security_notes+semantic.missing_or_ambiguous_information) or '—'}
    catalog[:]=[x for x in catalog if x.get('sop_id')!=sid];catalog.append(rec);catalog.sort(key=lambda x:x['sop_id']);atomic_json(catalog_path,catalog)
    processed=root/args.processed_dir;processed.mkdir(parents=True,exist_ok=True)
    if source.exists():
        dest=processed/source.name
        if dest.exists(): dest=processed/f'{source.stem}_{datetime.now().strftime("%Y%m%d_%H%M%S")}{source.suffix}'
        shutil.move(str(source),str(dest))
    req['status']='COMPLETE';req['completed_at_utc']=utc();req['sop_id']=sid;atomic_json(job_dir/'request.json',req)
    return {'job_id':job_dir.name,'status':'COMPLETE','sop_id':sid,'tags':semantic.tags,'tag_assignment_source':tag_source}
def run(args):
    root=Path(__file__).resolve().parent
    pending=root/args.pending_dir;pending.mkdir(parents=True,exist_ok=True)
    if args.resume:
        return [materialize(d,root,args) for d in sorted(pending.iterdir()) if d.is_dir()]
    inbox=root/args.input;inbox.mkdir(parents=True,exist_ok=True)
    registry=load_tag_registry(root/args.metadata_workbook)
    tags=registry['approved_tags'] or load(root/args.tags,[])
    if not tags:
        raise RuntimeError('No approved tags found. Supply SOP_Metadata_All.xlsx or approved_tags.json.')
    atomic_json(root/args.tags,tags)
    results=[]
    for source in sorted(p for p in inbox.rglob('*') if p.is_file() and p.suffix.lower() in SUPPORTED):
        jid=job_id(source);jdir=pending/jid;jdir.mkdir(parents=True,exist_ok=True)
        try:
            prepared=prepare_visual_document(source,jdir/'prepared')
            req=create_request(source,prepared,tags,jdir)
            results.append(call_llm_placeholder(req))
        except Exception as e: results.append({'job_id':jid,'status':'PREPARATION_FAILED','error':f'{type(e).__name__}: {e}'})
    return results
def main():
    p=argparse.ArgumentParser(description='Prepare SOP LLM jobs and stop with LLM_REQUIRED until a response is supplied.')
    p.add_argument('--input',default='eDelivery SOP');p.add_argument('--pending-dir',default='llm_pending')
    p.add_argument('--processed-dir',default='processed_SOP');p.add_argument('--catalog',default='SOP_JSON.json')
    p.add_argument('--content-dir',default='sop_content');p.add_argument('--filter-dir',default='sop_filter_content');p.add_argument('--tags',default='approved_tags.json')
    p.add_argument('--metadata-workbook',default='SOP_Metadata_All.xlsx',help='Workbook containing Tags and SOP Metadata worksheets.')
    p.add_argument('--resume',action='store_true',help='Validate imported llm_response.json files and complete catalog/output generation.')
    args=p.parse_args();results=run(args);print(json.dumps({'workflow':'offline_llm','processed_at_utc':utc(),'results':results},indent=2))
if __name__=='__main__':main()
