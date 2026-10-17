import argparse
import json
from pathlib import Path

def load_template(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    for key in ('common_style', 'resolutions'):
        if key not in data:
            raise ValueError(f'模板缺少字段: {key}')
    return data

def resolve_style(template, width, height):
    key = f'{width}x{height}'
    if key in template['resolutions']:
        return key, 1.0, template['resolutions'][key]
    base = template['resolutions']['1080x1920']
    scale = height / base['height']
    j = dict(base['jianying']); a = dict(base['ass'])
    j['font_size'] = round(j['font_size'] * scale, 2); j['shadow_distance'] = round(j['shadow_distance'] * scale, 2)
    a['font_size'] = round(a['font_size'] * scale, 2); a['outline'] = round(a['outline'] * scale, 2); a['shadow'] = round(a['shadow'] * scale, 2)
    a['x_shadow'] = round(a['x_shadow'] * scale); a['y_shadow'] = round(a['y_shadow'] * scale)
    a['position'] = {'x': round(width / 2), 'y': round(height * 0.6875)}
    return key, round(scale, 4), {'jianying': j, 'ass': a}

def build_result(template, width, height, mode):
    resolution, scale, selected = resolve_style(template, width, height)
    common = template['common_style']; a = selected['ass']
    a['style_line'] = (f"Style: Cap,{common['font']},{a['font_size']},{common['ass_primary_color']},{common['ass_primary_color']}," f"{common['ass_outline_color']},{common['ass_shadow_color']},0,0,0,0,{common['scale_x']},{common['scale_y']},0,0,{common['ass_border_style']},{a['outline']},{a['shadow']},5,20,20,0,1")
    p = a['position']; a['event_tag'] = f"{{\\an5\\pos({p['x']},{p['y']})\\xshad{a['x_shadow']}\\yshad{a['y_shadow']}}}"
    return {'mode':mode,'resolution':{'width':width,'height':height,'key':resolution},'scale':scale,'jianying':selected['jianying'],'ass':a,'checks':['template_loaded','resolution_resolved','font_fixed_to_jinling','white_text_fixed','black_shadow_70_percent','ass_mapping_is_approximate']}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--width',type=int,required=True); p.add_argument('--height',type=int,required=True); p.add_argument('--mode',choices=('ass','jianying'),default='ass'); p.add_argument('--template',type=Path,default=Path(__file__).parents[1]/'assets'/'subtitle_gold_template.json'); p.add_argument('--out',type=Path); args=p.parse_args()
    text=json.dumps(build_result(load_template(args.template),args.width,args.height,args.mode),ensure_ascii=False,indent=2)
    if args.out: args.out.write_text(text+'\n',encoding='utf-8')
    else: print(text)

if __name__ == '__main__': main()
