# classes for adding modifications to images during training
# such as the zoom in stuff
# or masking parts of original image onto currently generated image

import abc
from enum import Enum, auto
import Hallucinator
import imageUtils
import GenerateJob
import sys
import os
import random
import numpy as np

import makeCutouts
import imageUtils

import torch
from torch.nn import functional as F
from torchvision import transforms
from torchvision.transforms import functional as TF
from torch.cuda import get_device_properties
import imageio

from PIL import ImageFile, Image, PngImagePlugin, ImageChops
ImageFile.LOAD_TRUNCATED_IMAGES = True

 

class ImageModStage(Enum):
    PreTrain = auto() #pretraining step, mods happen before the next training step
    FinishedGeneration = auto() #happens when the generation of the image is finished



class IImageMod(metaclass=abc.ABCMeta):

    @classmethod
    def __subclasshook__(cls, subclass):
        return (hasattr(subclass, 'ShouldApply') and 
                callable(subclass.ShouldApply) and 
                hasattr(subclass, 'OnPreTrain') and 
                callable(subclass.OnPreTrain) or 
                NotImplemented)

    def __init__(self, GenJob, startIt: int = 0, endIt: int = 9999999999, freq: int = 10):
        super().__init__()

        self.GenJob = GenJob        
        self.freq = freq
        self.startIt = startIt
        self.endIt = endIt

    @abc.abstractmethod
    def Initialize(self):
        raise NotImplementedError

    @abc.abstractmethod
    def ShouldApply(self, stage: ImageModStage, iteration: int ) -> bool:
        return self.IsIterationInRange(iteration) and iteration % self.freq == 0

    def IsIterationInRange(self, iteration: int) -> bool:
        if iteration < self.startIt or iteration >self.endIt:
            return False 
        return True

    @abc.abstractmethod
    def OnPreTrain(self, iteration: int ):
        raise NotImplementedError






class OriginalImageMask(IImageMod):

    def __init__(self, GenJob, startIt: int = 0, endIt: int = 9999999999, freq: int = 10, maskPath: str = ''):
        super().__init__(GenJob, startIt, endIt, freq)

        self.maskPath = maskPath
        self.original_image_tensor = None
        self.image_mask_tensor = None
        self.image_mask_tensor_invert = None

    def Initialize(self):
        original_pil = self.GenJob.GerCurrentImageAsPIL()
        self.original_image_tensor = TF.to_tensor(original_pil).to(self.GenJob.vqganDevice)

        img = Image.open(self.maskPath)
        pil_image = img.convert('RGB')
        
        image_mask_np  = np.asarray(pil_image)

        #makes float32 mask
        self.image_mask_tensor = TF.to_tensor(image_mask_np).to(self.GenJob.vqganDevice)

        #make boolean masks
        self.image_mask_tensor_invert = torch.logical_not( self.image_mask_tensor )
        self.image_mask_tensor = torch.logical_not( self.image_mask_tensor_invert )


    def ShouldApply(self, stage: ImageModStage, iteration: int ) -> bool:
        if stage.value != ImageModStage.PreTrain.value:
            return False

        return super().ShouldApply(stage, iteration)


    def OnPreTrain(self, iteration: int ):
        with torch.inference_mode():
            curQuantImg = self.GenJob.synth(self.GenJob.quantizedImage, self.GenJob.vqganGumbelEnabled)

            #this removes the first dim sized 1 to match the rest
            curQuantImg = torch.squeeze(curQuantImg)

            keepCurrentImg = curQuantImg * self.image_mask_tensor_invert.int().float()
            keepOrig = self.original_image_tensor * self.image_mask_tensor.int().float()
            pil_tensor = keepCurrentImg + keepOrig

        # Re-encode original?
        self.GenJob.quantizedImage, *_ = self.GenJob.vqganModel.encode(pil_tensor.to(self.GenJob.vqganDevice).unsqueeze(0) * 2 - 1)
        #self.GenJob.original_quantizedImage = self.GenJob.quantizedImage.detach()
        
        self.GenJob.quantizedImage.requires_grad_(True)
        self.GenJob.optimiser = self.GenJob.hallucinatorInst.get_optimiser(self.GenJob.quantizedImage, self.GenJob.config.optimiser, self.GenJob.config.step_size)






class ImageZoomer(IImageMod):
    #fucking python has no maxint to use as a large value, annoying
    def __init__(self, GenJob, startIt: int = 0, endIt: int = 9999999999, freq: int = 10, zoom_scale: float = 0.99, zoom_shift_x: int = 0, zoom_shift_y: int = 0):
        super().__init__(GenJob, startIt, endIt, freq)

        ##need to make these configurable
        self.zoom_scale = zoom_scale
        self.zoom_shift_x = zoom_shift_x
        self.zoom_shift_y = zoom_shift_y

    def Initialize(self):
        pass


    def ShouldApply(self, stage: ImageModStage, iteration: int ) -> bool:
        if stage.value != ImageModStage.PreTrain.value:
            return False

        return super().ShouldApply(stage, iteration)


    def OnPreTrain(self, iteration: int ):
        with torch.inference_mode():
            out = self.GenJob.synth(self.GenJob.quantizedImage, self.GenJob.vqganGumbelEnabled)
            
            # Save image
            img = np.array(out.mul(255).clamp(0, 255)[0].cpu().detach().numpy().astype(np.uint8))[:,:,:]
            img = np.transpose(img, (1, 2, 0))
            
            # Convert NP to Pil image
            pil_image = Image.fromarray(np.array(img).astype('uint8'), 'RGB')
                                    
            # Zoom
            if self.zoom_scale != 1:
                pil_image_zoom = imageUtils.zoom_at(pil_image, self.GenJob.ImageSizeX/2, self.GenJob.ImageSizeY/2, self.zoom_scale)
            else:
                pil_image_zoom = pil_image
            
            # Shift - https://pillow.readthedocs.io/en/latest/reference/ImageChops.html
            if self.zoom_shift_x or self.zoom_shift_y:
                # This one wraps the image
                pil_image_zoom = ImageChops.offset(pil_image_zoom, self.zoom_shift_x, self.zoom_shift_y)
            
            # Convert image back to a tensor again
            pil_tensor = TF.to_tensor(pil_image_zoom)

        # Re-encode original?
        self.GenJob.quantizedImage, *_ = self.GenJob.vqganModel.encode(pil_tensor.to(self.GenJob.vqganDevice).unsqueeze(0) * 2 - 1)
        #self.GenJob.original_quantizedImage = self.GenJob.quantizedImage.detach()
        
        self.GenJob.quantizedImage.requires_grad_(True)
        self.GenJob.optimiser = self.GenJob.hallucinatorInst.get_optimiser(self.GenJob.quantizedImage, self.GenJob.config.optimiser, self.GenJob.config.step_size)



class ImageRotate(IImageMod):
    #fucking python has no maxint to use as a large value, annoying
    def __init__(self, GenJob, startIt: int = 0, endIt: int = 9999999999, freq: int = 10, angle: int = 1):
        super().__init__(GenJob, startIt, endIt, freq)

        self.angle = angle

    def Initialize(self):
        pass


    def ShouldApply(self, stage: ImageModStage, iteration: int ) -> bool:
        if stage.value != ImageModStage.PreTrain.value:
            return False

        return super().ShouldApply(stage, iteration)


    def OnPreTrain(self, iteration: int ):
        with torch.inference_mode():
            curQuantImg = self.GenJob.synth(self.GenJob.quantizedImage, self.GenJob.vqganGumbelEnabled)

            #this removes the first dim sized 1 to match the rest
            curQuantImg = torch.squeeze(curQuantImg)

            pil_tensor = TF.rotate(curQuantImg, self.angle)

        # Re-encode original?
        self.GenJob.quantizedImage, *_ = self.GenJob.vqganModel.encode(pil_tensor.to(self.GenJob.vqganDevice).unsqueeze(0) * 2 - 1)
        #self.GenJob.original_quantizedImage = self.GenJob.quantizedImage.detach()
        
        self.GenJob.quantizedImage.requires_grad_(True)
        self.GenJob.optimiser = self.GenJob.hallucinatorInst.get_optimiser(self.GenJob.quantizedImage, self.GenJob.config.optimiser, self.GenJob.config.step_size)                