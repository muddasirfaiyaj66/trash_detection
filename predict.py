from ultralytics import YOLO
model =YOLO("best.pt")

# model.predict(source="imge/video/cam",show=True,save=True,conf=0.6,line_width=2,save_crop=True,save_text=True,show_labels=True,show_conf=True,classes=[0,1])
model.predict(source="0",show=True,conf=0.6,line_width=2,classes=[2,3])


# model.export(format="onnx")
