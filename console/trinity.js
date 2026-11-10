'use strict';
/* TRINITY stage: three AI cores feeding one fused core. Pure WebGL2, no libraries, no external assets (CSP: self).
   Nothing here is decorative-random: every speed, brightness and colour is driven through Trinity.set() by the
   live work ledger (/ops/activity). Slots are roles: 0 HEAD (top), 1 BUILDER (left), 2 REVIEWER (right). */
window.Trinity=(function(){
const SLOTS=[[0,.98,0],[-1.02,-.60,0],[1.02,-.60,0]];
const COUNTS={ribbon:7000,stream:4500,node:3200,dust:5200,edge:1700,nucleus:9000,verify:2400,shock:1800};

const VS_POINTS=`#version 300 es
precision highp float;
layout(location=0) in vec4 aSeed;
layout(location=1) in vec2 aMeta;
uniform mat4 uVP;
uniform float uTime,uPx,uCore,uVerify,uPulse,uBoot,uPhC;
uniform vec3 uSlot[3];
uniform vec3 uColor[3];
uniform vec3 uAct,uEdge,uPhS,uPhE,uPhN,uPulseColor;
out vec3 vColor;
out float vAlpha;
const float TAU=6.28318530718;
vec3 rotX(vec3 p,float a){float c=cos(a),s=sin(a);return vec3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);}
vec3 rotY(vec3 p,float a){float c=cos(a),s=sin(a);return vec3(c*p.x+s*p.z,p.y,-s*p.x+c*p.z);}
vec3 rotZ(vec3 p,float a){float c=cos(a),s=sin(a);return vec3(c*p.x-s*p.y,s*p.x+c*p.y,p.z);}
vec3 sph(float u,float v){float z=2.*u-1.;float r=sqrt(max(0.,1.-z*z));float a=TAU*v;return vec3(r*cos(a),z,r*sin(a));}
float pick(vec3 v,int i){return i==0?v.x:(i==1?v.y:v.z);}
void main(){
  float kind=aMeta.x;int o=int(aMeta.y+.5);
  vec3 col=uColor[o];float act=pick(uAct,o);float t=uTime;
  vec3 p=vec3(0.);float size=.014;float alpha=1.;
  float energy=clamp(uCore+uPulse*.6,0.,1.4);
  if(kind<.5){                                   // 0 fused core: one orbital ribbon per AI, intertwined
    float th=TAU*aSeed.x+uPhC*(.6+.8*aSeed.w);
    float w=aSeed.y-.5;
    float tw=th*1.5+t*.2+float(o)*2.1;
    float R=.47+.03*(aSeed.z-.5)+.028*sin(th*5.+t*1.7+float(o)*2.)*(.3+energy);
    float W=.105*(1.+.6*energy);
    vec3 l=vec3(cos(th)*(R+w*W*cos(tw)),w*W*sin(tw),sin(th)*(R+w*W*cos(tw)));
    p=rotY(rotX(l,1.18),float(o)*TAU/3.+t*.07);
    size=.009+.012*aSeed.z;alpha=.34+.36*min(energy,1.);
    col=mix(col,vec3(1.),(.04+.22*min(energy,1.))*aSeed.z*aSeed.z);
  }else if(kind<1.5){                            // 1 stream: an AI pours its work into the core
    float u=fract(aSeed.x+pick(uPhS,o)*(.6+.8*aSeed.y));
    vec3 a=uSlot[o];float e=u*u*(3.-2.*u);
    vec3 base=a*(1.-e);
    vec3 dir=normalize(-a);vec3 n1=normalize(cross(dir,vec3(0.,0.,1.)));vec3 n2=cross(dir,n1);
    float ang=aSeed.z*TAU+u*7.+t*.5;
    float rad=(.012+.12*aSeed.w*aSeed.w)*sin(3.14159*u)*(.65+.7*act);
    p=base+(n1*cos(ang)+n2*sin(ang))*rad;
    float vis=step(aSeed.w,.2+.8*act);          // idle: only a thin thread stays lit
    alpha=vis*(.16+.84*act)*smoothstep(0.,.07,u)*(1.-.6*smoothstep(.82,1.,u));
    size=(.010+.017*aSeed.w)*(.8+.5*act);
    col=mix(col,vec3(1.),e*e*.35*uCore);
  }else if(kind<2.5){                            // 2 AI node: dense body + orbit ring
    vec3 c=uSlot[o];
    if(aSeed.z<.72){
      vec3 d=sph(aSeed.x,aSeed.y);
      float rr=(.085+.055*aSeed.w)*(1.+.32*act+.06*sin(t*2.2+aSeed.w*TAU)*(.4+act));
      d=rotY(rotX(d,t*.3+float(o)),t*.45);
      p=c+d*rr;col=mix(col,vec3(1.),.28*(1.-aSeed.w));
      alpha=.30+.5*act;size=.011+.013*aSeed.w;
    }else{
      float th=TAU*aSeed.x+pick(uPhN,o)*(.8+.4*aSeed.y);
      float rr=.19+.022*(aSeed.w-.5)+.03*act;
      vec3 l=vec3(cos(th)*rr,(aSeed.y-.5)*.014,sin(th)*rr);
      p=c+rotZ(rotX(l,1.2),float(o)*2.1+t*.1);
      alpha=(.22+.78*act)*(.4+.6*pow(aSeed.x,3.));size=.010;
    }
  }else if(kind<3.5){                            // 3 depth dust
    p=(aSeed.xyz*2.-1.)*vec3(3.8,2.3,2.8);p=rotY(p,t*.012);p.y+=sin(t*.07+aSeed.w*TAU)*.08;
    alpha=.09+.22*pow(.5+.5*sin(t*(.3+aSeed.w)+aSeed.x*40.),3.);
    size=.008+.016*aSeed.w*aSeed.w;col=mix(vec3(.5,.66,1.),vec3(1.),aSeed.z);
  }else if(kind<4.5){                            // 4 hand-off arc: slot o -> next slot (order, code, verdict)
    int n=(o+1)%3;vec3 a=uSlot[o],b=uSlot[n];vec3 m=(a+b)*.5;vec3 ctrl=m+normalize(m)*.50;
    float edge=pick(uEdge,o);
    float u=fract(aSeed.x+pick(uPhE,o)*(.75+.5*aSeed.y));
    p=mix(mix(a,ctrl,u),mix(ctrl,b,u),u)+(aSeed.yzw-.5)*.03*(.4+edge);
    float band=pow(.5+.5*sin(aSeed.x*TAU*4.),4.);
    alpha=(.045+.95*edge)*(.25+.75*band)*smoothstep(0.,.06,u)*smoothstep(1.,.94,u);
    col=mix(uColor[o],uColor[n],u);size=.009+.016*aSeed.w*(.3+edge);
  }else if(kind<5.5){                            // 5 nucleus: where the three become one
    vec3 d=sph(aSeed.x,aSeed.y);
    float rr=(.035+.115*pow(aSeed.z,1.5))*(1.+.26*uPulse+.10*sin(t*1.6+aSeed.w*TAU)*(.3+uCore));
    d=rotY(rotZ(d,t*.21+aSeed.w),uPhC*.5);p=d*rr;
    col=mix(col,vec3(1.,.96,.9),.3+.62*uCore*(1.-aSeed.z));
    alpha=.22+.5*uCore;size=.010+.017*(1.-aSeed.z);
  }else if(kind<6.5){                            // 6 verification scanner (Director running tests)
    float sgn=aSeed.y<.5?1.:-1.;
    float th=TAU*aSeed.x+t*1.3*sgn;float rr=.70+.016*(aSeed.w-.5);
    vec3 l=vec3(cos(th)*rr,0.,sin(th)*rr);
    p=rotX(l,1.57+.55*(sgn>0.?sin(t*.9):cos(t*.7)));
    alpha=uVerify*(.10+.90*pow(aSeed.x,5.));col=mix(vec3(.55,1.,.82),vec3(1.),pow(aSeed.x,8.));size=.012;
  }else{                                         // 7 shockwave on a result
    float th=TAU*aSeed.x;float k=1.-uPulse;float rr=.25+k*2.4+(aSeed.w-.5)*.06;
    p=vec3(cos(th)*rr,sin(th)*rr,(aSeed.y-.5)*.06);
    alpha=uPulse*uPulse*(.6+.4*aSeed.z);col=uPulseColor;size=.011+.012*aSeed.z;
  }
  vec3 scatter=(aSeed.zxy*2.-1.)*3.2;            // boot: everything assembles out of scattered dust
  float bt=clamp(uBoot*1.3-aSeed.w*.3,0.,1.);bt=bt*bt*(3.-2.*bt);
  p=mix(scatter,p,bt);alpha*=bt;
  vec4 clip=uVP*vec4(p,1.);
  gl_Position=clip;gl_PointSize=max(1.5,size*uPx/clip.w);
  vColor=col;vAlpha=alpha;
}`;
const FS_POINTS=`#version 300 es
precision highp float;
in vec3 vColor;in float vAlpha;out vec4 o;
void main(){vec2 c=gl_PointCoord*2.-1.;float d=dot(c,c);if(d>1.)discard;float a=exp(-d*4.2)*vAlpha;o=vec4(vColor*a,a);}`;
const VS_FULL=`#version 300 es
out vec2 vUv;
void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));vUv=p;gl_Position=vec4(p*2.-1.,0.,1.);}`;
const FS_BLUR=`#version 300 es
precision highp float;
in vec2 vUv;out vec4 o;uniform sampler2D uTex;uniform vec2 uDir;
void main(){
  vec4 c=texture(uTex,vUv)*.227027;
  c+=(texture(uTex,vUv+uDir*1.3846153)+texture(uTex,vUv-uDir*1.3846153))*.3162162;
  c+=(texture(uTex,vUv+uDir*3.2307692)+texture(uTex,vUv-uDir*3.2307692))*.0702703;
  o=c;
}`;
const FS_COMP=`#version 300 es
precision highp float;
in vec2 vUv;out vec4 o;
uniform sampler2D uScene,uB1,uB2;
uniform vec2 uRes,uCenter;uniform float uTime,uCore,uPulse;uniform vec3 uTint,uPulseColor;
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
  return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),f.x),f.y);}
float fbm(vec2 p){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*noise(p);p=p*2.03+vec2(11.3,7.7);a*=.5;}return v;}
void main(){
  vec2 q=(vUv-uCenter)*vec2(uRes.x/uRes.y,1.);float r=length(q);
  vec3 bg=mix(vec3(.016,.024,.048),vec3(.004,.006,.013),smoothstep(0.,1.15,r));
  float neb=fbm(q*1.7+vec2(uTime*.012,-uTime*.008));float neb2=fbm(q*3.1-vec2(uTime*.01,0.)+neb);
  bg+=uTint*(.035+.10*uCore)*neb*neb2*(1.-smoothstep(.05,1.3,r))*1.6;
  float ring=smoothstep(.0035,0.,abs(r-.36))+smoothstep(.0035,0.,abs(r-.72))*.6+smoothstep(.003,0.,abs(r-1.08))*.3;
  bg+=vec3(.35,.5,.8)*ring*.045;
  bg+=uPulseColor*uPulse*.10*(1.-smoothstep(0.,1.2,r));
  vec2 ca=(vUv-.5)*.0016;
  vec3 scene=vec3(texture(uScene,vUv+ca).r,texture(uScene,vUv).g,texture(uScene,vUv-ca).b);
  vec3 col=bg+scene+texture(uB1,vUv).rgb*.42+texture(uB2,vUv).rgb*.70;
  float peak=max(col.r,max(col.g,col.b))+1e-4;                 // tone-map on the peak channel: bright stays coloured
  vec3 hue=col*((1.-exp(-peak*1.15))/peak);
  col=mix(hue,1.-exp(-col*1.15),.38);
  vec2 v=vUv-.5;col*=1.-.42*dot(v,v)*1.6;
  col+=(hash(vUv*uRes+fract(uTime)*91.7)-.5)*.022;
  o=vec4(max(col,0.),1.);
}`;

let canvas,gl,progP,progB,progC,uP={},uB={},uC={},vao,total=0,fb={},hdr=false,size=[0,0],running=false,raf=0;
let onFrame=null,failed=false,flat=null,listening=false;
const cur={act:[.1,.1,.1],edge:[0,0,0],core:.18,verify:0,colors:[[1,.54,.36],[.42,.65,1],[.24,.95,.75]]};
const tgt={act:[.1,.1,.1],edge:[0,0,0],core:.18,verify:0,colors:cur.colors.map(c=>c.slice())};
const ph={c:0,s:[0,0,0],e:[0,0,0],n:[0,0,0]};
let pulse=0,pulseColor=[1,1,1],boot=0,time=0,last=0,timeScale=1;
let rect={x:0,y:0,w:1,h:1};const mouse=[0,0],mouseS=[0,0];
const vp=new Float32Array(16);

function compile(type,src){const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s)||'shader');return s;}
function program(vs,fs){const p=gl.createProgram();gl.attachShader(p,compile(gl.VERTEX_SHADER,vs));gl.attachShader(p,compile(gl.FRAGMENT_SHADER,fs));
  gl.bindAttribLocation(p,0,'aSeed');gl.bindAttribLocation(p,1,'aMeta');gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p)||'link');return p;}
function uniforms(p,names){const o={};for(const n of names)o[n]=gl.getUniformLocation(p,n);return o;}
function rng(seed){let s=seed>>>0;return()=>{s=(s+0x6D2B79F5)|0;let t=Math.imul(s^s>>>15,1|s);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}

function buildGeometry(){
  const groups=[];const add=(kind,owner,n)=>groups.push([kind,owner,n]);
  for(let o=0;o<3;o++){add(0,o,COUNTS.ribbon);add(1,o,COUNTS.stream);add(2,o,COUNTS.node);add(4,o,COUNTS.edge);add(5,o,COUNTS.nucleus/3);}
  add(3,0,COUNTS.dust);add(6,0,COUNTS.verify);add(7,0,COUNTS.shock);
  total=groups.reduce((a,g)=>a+g[2],0);
  const data=new Float32Array(total*6),rand=rng(20260930);let i=0;
  for(const [kind,owner,n] of groups)for(let k=0;k<n;k++){data[i++]=rand();data[i++]=rand();data[i++]=rand();data[i++]=rand();data[i++]=kind;data[i++]=owner;}
  vao=gl.createVertexArray();gl.bindVertexArray(vao);
  const buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,4,gl.FLOAT,false,24,0);
  gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,2,gl.FLOAT,false,24,16);
  gl.bindVertexArray(null);
}
function target(w,h){
  const tex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,tex);
  if(hdr)gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA16F,w,h,0,gl.RGBA,gl.HALF_FLOAT,null);
  else gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA8,w,h,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
  for(const [k,v] of [[gl.TEXTURE_MIN_FILTER,gl.LINEAR],[gl.TEXTURE_MAG_FILTER,gl.LINEAR],[gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE],[gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE]])gl.texParameteri(gl.TEXTURE_2D,k,v);
  const f=gl.createFramebuffer();gl.bindFramebuffer(gl.FRAMEBUFFER,f);gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,tex,0);
  const ok=gl.checkFramebufferStatus(gl.FRAMEBUFFER)===gl.FRAMEBUFFER_COMPLETE;gl.bindFramebuffer(gl.FRAMEBUFFER,null);
  return {f,tex,w,h,ok};
}
function freeTargets(){for(const k of Object.keys(fb)){gl.deleteFramebuffer(fb[k].f);gl.deleteTexture(fb[k].tex);}fb={};}
function resize(){
  const cw=canvas.clientWidth||1,ch=canvas.clientHeight||1;
  const dpr=Math.min(window.devicePixelRatio||1,1.5,Math.sqrt(3.4e6/(cw*ch)));     // cap the HDR buffers at ~3.4 MP
  const w=Math.max(2,Math.round(cw*dpr)),h=Math.max(2,Math.round(ch*dpr));
  if(w===size[0]&&h===size[1])return;
  size=[w,h];canvas.width=w;canvas.height=h;
  if(!gl)return;
  freeTargets();
  fb.scene=target(w,h);
  if(!fb.scene.ok&&hdr){hdr=false;freeTargets();fb.scene=target(w,h);}
  const w2=Math.max(2,w>>1),h2=Math.max(2,h>>1),w4=Math.max(2,w>>2),h4=Math.max(2,h>>2);
  fb.a1=target(w2,h2);fb.b1=target(w2,h2);fb.a2=target(w4,h4);fb.b2=target(w4,h4);
}
// ---- camera (column-major mat4)
function mul(o,a,b){for(let c=0;c<4;c++)for(let r=0;r<4;r++)o[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3];return o;}
const P=new Float32Array(16),V=new Float32Array(16);
function camera(){
  const W=size[0],H=size[1],aspect=W/H,fov=36*Math.PI/180,f=1/Math.tan(fov/2);
  const need=Math.max(1.36/(Math.max(rect.h,.2)),1.50/(aspect*Math.max(rect.w,.2)));
  const dist=need*f,near=.1,far=40;
  P.fill(0);P[0]=f/aspect;P[5]=f;P[10]=(far+near)/(near-far);P[11]=-1;P[14]=2*far*near/(near-far);
  const yaw=.24*Math.sin(time*.11)+mouseS[0]*.22,pitch=.07*Math.sin(time*.17)+mouseS[1]*.12;
  const tx=0,ty=.06,tz=0;
  const ex=tx+dist*Math.sin(yaw)*Math.cos(pitch),ey=ty+dist*Math.sin(pitch),ez=tz+dist*Math.cos(yaw)*Math.cos(pitch);
  let zx=ex-tx,zy=ey-ty,zz=ez-tz;const zl=Math.hypot(zx,zy,zz);zx/=zl;zy/=zl;zz/=zl;
  let xx=zz,xy=0,xz=-zx;const xl=Math.hypot(xx,xy,xz);xx/=xl;xz/=xl;                  // up(0,1,0) x z
  const yx=zy*xz-zz*xy,yy=zz*xx-zx*xz,yz=zx*xy-zy*xx;
  V[0]=xx;V[1]=yx;V[2]=zx;V[3]=0;V[4]=xy;V[5]=yy;V[6]=zy;V[7]=0;V[8]=xz;V[9]=yz;V[10]=zz;V[11]=0;
  V[12]=-(xx*ex+xy*ey+xz*ez);V[13]=-(yx*ex+yy*ey+yz*ez);V[14]=-(zx*ex+zy*ey+zz*ez);V[15]=1;
  mul(vp,P,V);
  const ox=2*(rect.x+rect.w/2)-1,oy=1-2*(rect.y+rect.h/2);                            // lens shift: scene centre -> stage rect centre
  for(let c=0;c<4;c++){vp[c*4]+=ox*vp[c*4+3];vp[c*4+1]+=oy*vp[c*4+3];}
  return f;
}
function project(p){
  const x=vp[0]*p[0]+vp[4]*p[1]+vp[8]*p[2]+vp[12],y=vp[1]*p[0]+vp[5]*p[1]+vp[9]*p[2]+vp[13],w=vp[3]*p[0]+vp[7]*p[1]+vp[11]*p[2]+vp[15];
  return [(x/w*.5+.5),(1-(y/w*.5+.5)),w];
}
function approach(dt){
  const k=1-Math.exp(-dt*3.2),kc=1-Math.exp(-dt*2.2);
  for(let i=0;i<3;i++){cur.act[i]+=(tgt.act[i]-cur.act[i])*k;cur.edge[i]+=(tgt.edge[i]-cur.edge[i])*k;
    for(let j=0;j<3;j++)cur.colors[i][j]+=(tgt.colors[i][j]-cur.colors[i][j])*kc;}
  cur.core+=(tgt.core-cur.core)*k;cur.verify+=(tgt.verify-cur.verify)*k;
  mouseS[0]+=(mouse[0]-mouseS[0])*k*.6;mouseS[1]+=(mouse[1]-mouseS[1])*k*.6;
  pulse=Math.max(0,pulse-dt/1.7);
  ph.c+=dt*(.16+.8*cur.core+.7*pulse);
  for(let i=0;i<3;i++){ph.s[i]+=dt*(.03+.34*cur.act[i]);ph.e[i]+=dt*(.045+.32*cur.edge[i]);ph.n[i]+=dt*(.22+1.7*cur.act[i]);}
  boot=Math.min(1,boot+dt/3.2);
}
function blur(src,dst,dx,dy){
  gl.bindFramebuffer(gl.FRAMEBUFFER,dst.f);gl.viewport(0,0,dst.w,dst.h);
  gl.bindTexture(gl.TEXTURE_2D,src.tex);gl.uniform2f(uB.uDir,dx/dst.w,dy/dst.h);gl.drawArrays(gl.TRIANGLES,0,3);
}
function frame(now){
  raf=requestAnimationFrame(frame);
  const dt=Math.min(.05,Math.max(0,(now-last)/1000))*timeScale;last=now;
  render(dt);
}
function render(dt){
  time+=dt;resize();approach(dt);
  const f=camera(),tint=[0,0,0];let wsum=0;
  for(let i=0;i<3;i++){const w=.25+cur.act[i];wsum+=w;for(let j=0;j<3;j++)tint[j]+=cur.colors[i][j]*w;}
  if(flat){drawFlat();emit();return;}
  gl.bindFramebuffer(gl.FRAMEBUFFER,fb.scene.f);gl.viewport(0,0,size[0],size[1]);
  gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
  gl.enable(gl.BLEND);gl.blendFunc(gl.ONE,gl.ONE);
  gl.useProgram(progP);gl.bindVertexArray(vao);
  gl.uniformMatrix4fv(uP.uVP,false,vp);gl.uniform1f(uP.uTime,time);gl.uniform1f(uP.uPx,size[1]*f*.5);
  gl.uniform1f(uP.uCore,cur.core);gl.uniform1f(uP.uVerify,cur.verify);gl.uniform1f(uP.uPulse,pulse);gl.uniform1f(uP.uBoot,boot);gl.uniform1f(uP.uPhC,ph.c);
  gl.uniform3fv(uP['uSlot[0]'],SLOTS.flat());gl.uniform3fv(uP['uColor[0]'],cur.colors.flat());
  gl.uniform3fv(uP.uAct,cur.act);gl.uniform3fv(uP.uEdge,cur.edge);gl.uniform3fv(uP.uPhS,ph.s);gl.uniform3fv(uP.uPhE,ph.e);gl.uniform3fv(uP.uPhN,ph.n);
  gl.uniform3fv(uP.uPulseColor,pulseColor);
  gl.drawArrays(gl.POINTS,0,total);
  gl.disable(gl.BLEND);gl.bindVertexArray(null);
  gl.useProgram(progB);gl.activeTexture(gl.TEXTURE0);gl.uniform1i(uB.uTex,0);
  blur(fb.scene,fb.a1,1,0);blur(fb.a1,fb.b1,0,1);blur(fb.b1,fb.a2,1.6,0);blur(fb.a2,fb.b2,0,1.6);
  gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.viewport(0,0,size[0],size[1]);
  gl.useProgram(progC);
  gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,fb.scene.tex);gl.uniform1i(uC.uScene,0);
  gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D,fb.b1.tex);gl.uniform1i(uC.uB1,1);
  gl.activeTexture(gl.TEXTURE2);gl.bindTexture(gl.TEXTURE_2D,fb.b2.tex);gl.uniform1i(uC.uB2,2);
  const c=project([0,0,0]);
  gl.uniform2f(uC.uRes,size[0],size[1]);gl.uniform2f(uC.uCenter,c[0],1-c[1]);gl.uniform1f(uC.uTime,time);
  gl.uniform1f(uC.uCore,cur.core);gl.uniform1f(uC.uPulse,pulse);
  gl.uniform3f(uC.uTint,tint[0]/wsum,tint[1]/wsum,tint[2]/wsum);gl.uniform3fv(uC.uPulseColor,pulseColor);
  gl.drawArrays(gl.TRIANGLES,0,3);gl.activeTexture(gl.TEXTURE0);
  emit();
}
function emit(){
  if(!onFrame)return;
  const cw=canvas.clientWidth,ch=canvas.clientHeight,unit=project([.5,0,0]),zero=project([0,0,0]);
  onFrame({slots:SLOTS.map(s=>{const p=project(s);return [p[0]*cw,p[1]*ch];}),core:[zero[0]*cw,zero[1]*ch],
           unit:Math.abs(unit[0]-zero[0])*cw*2,act:cur.act,pulse});
}
// ---- 2D fallback (no WebGL2): same data, simpler picture
function drawFlat(){
  const c=flat,cw=canvas.clientWidth,ch=canvas.clientHeight,W=size[0],H=size[1],k=W/cw;
  c.setTransform(1,0,0,1,0,0);c.fillStyle='#05070d';c.fillRect(0,0,W,H);c.globalCompositeOperation='lighter';
  const pt=p=>{const q=project(p);return [q[0]*W,q[1]*H];},o=pt([0,0,0]),u=Math.abs(pt([.5,0,0])[0]-o[0])*2;
  const rgb=(col,a)=>'rgba('+col.map(v=>Math.round(Math.min(1,v)*255)).join(',')+','+a+')';
  const glow=(x,y,r,col,a)=>{const g=c.createRadialGradient(x,y,0,x,y,r);g.addColorStop(0,rgb(col,a));g.addColorStop(1,rgb(col,0));c.fillStyle=g;c.beginPath();c.arc(x,y,r,0,7);c.fill();};
  glow(o[0],o[1],u*(.5+.12*cur.core+.2*pulse),[1,.95,.9],.35+.4*cur.core);
  for(let i=0;i<3;i++){const s=pt(SLOTS[i]),col=cur.colors[i];glow(s[0],s[1],u*(.22+.1*cur.act[i]),col,.3+.6*cur.act[i]);
    for(let j=0;j<26;j++){const t=(j/26+ph.s[i])%1,e=t*t*(3-2*t);c.fillStyle=rgb(col,(.12+.8*cur.act[i])*Math.sin(Math.PI*t));c.beginPath();c.arc(s[0]+(o[0]-s[0])*e,s[1]+(o[1]-s[1])*e,2.2*k,0,7);c.fill();}
    c.strokeStyle=rgb(col,.5);c.lineWidth=1.5*k;c.beginPath();c.ellipse(o[0],o[1],u*.47,u*.16,i*Math.PI/3+ph.c*.2,0,7);c.stroke();}
  c.globalCompositeOperation='source-over';
}
function init(el,opts){
  canvas=el;onFrame=(opts&&opts.onFrame)||null;
  if(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches){timeScale=.2;boot=1;}
  try{
    gl=canvas.getContext('webgl2',{antialias:false,alpha:false,powerPreference:'high-performance'});
    if(!gl)throw Error('WebGL2 unavailable');
    hdr=!!(gl.getExtension('EXT_color_buffer_float')||gl.getExtension('EXT_color_buffer_half_float'));
    progP=program(VS_POINTS,FS_POINTS);progB=program(VS_FULL,FS_BLUR);progC=program(VS_FULL,FS_COMP);
    uP=uniforms(progP,['uVP','uTime','uPx','uCore','uVerify','uPulse','uBoot','uPhC','uSlot[0]','uColor[0]','uAct','uEdge','uPhS','uPhE','uPhN','uPulseColor']);
    uB=uniforms(progB,['uTex','uDir']);uC=uniforms(progC,['uScene','uB1','uB2','uRes','uCenter','uTime','uCore','uPulse','uTint','uPulseColor']);
    buildGeometry();
    if(!listening){
      canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();cancelAnimationFrame(raf);running=false;});
      canvas.addEventListener('webglcontextrestored',()=>{size=[0,0];fb={};init(canvas,{onFrame});});
    }
  }catch(err){
    failed=true;gl=null;flat=canvas.getContext('2d');
    if(window.console)console.warn('Trinity: WebGL2 fallback to 2D -',err.message);
  }
  if(!listening){listening=true;window.addEventListener('pointermove',e=>{mouse[0]=(e.clientX/window.innerWidth-.5)*2;mouse[1]=(e.clientY/window.innerHeight-.5)*2;},{passive:true});}
  size=[0,0];resize();
  if(!running){running=true;last=performance.now();raf=requestAnimationFrame(frame);}
  return {webgl:!failed,hdr};
}
function set(s){
  if(s.act)for(let i=0;i<3;i++)tgt.act[i]=s.act[i];
  if(s.edge)for(let i=0;i<3;i++)tgt.edge[i]=s.edge[i];
  if(s.core!=null)tgt.core=s.core;if(s.verify!=null)tgt.verify=s.verify;
  if(s.colors)for(let i=0;i<3;i++)tgt.colors[i]=s.colors[i].slice();
}
return {init,set,
  pulse(color){pulse=1;pulseColor=(color||[1,1,1]).slice();},
  focus(r){rect=r;},                                  // stage rect inside the canvas, normalised {x,y,w,h}
  step(sec){for(let s=0;s<sec;s+=1/30){time+=1/30;approach(1/30);}render(0);},   // deterministic advance (captures, hidden tabs)
  state(){return {cur,tgt,pulse,boot,time,size:size.slice(),hdr,webgl:!failed,points:total};}};
})();
