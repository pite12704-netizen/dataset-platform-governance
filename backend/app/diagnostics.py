"""Internal stack locations without exception messages, source lines or locals."""
import json
import logging
from pathlib import Path
import traceback


def report(logger,event,exc,**context):
    allowed={'request_id','job_id','method','path','operation'}
    frames=[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)[-16:]]
    data={'event':event,'exception':type(exc).__name__,'frames':frames,
          **{key:value for key,value in context.items() if key in allowed}}
    if isinstance(exc,OSError) and isinstance(exc.errno,int):data['errno']=exc.errno
    logger.log(logging.ERROR,json.dumps(data,ensure_ascii=False))
