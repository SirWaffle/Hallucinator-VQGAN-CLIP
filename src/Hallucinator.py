import sys
import os
import random
from typing import Tuple
import numpy as np
from tqdm import tqdm

import GenerationMods
import makeCutouts
import imageUtils
import GenerateJob

#stuff im using from source instead of installs
# i want to run clip from source, not an install. I have clip in a dir alongside this project
# so i append the parent dir to the proj and we expect to find a folder named clip there
sys.path.append('..\\')
from CLIP import clip


# pip install taming-transformers doesn't work with Gumbel, but does not yet work with coco etc
# appending the path does work with Gumbel
sys.path.append('taming-transformers')
from taming.models import cond_transformer, vqgan
from taming.modules.diffusionmodules import model



import yaml
from urllib.request import urlopen
import gc

from omegaconf import OmegaConf

import torch
from torch.cuda.amp import autocast
from torch.cuda.amp import custom_fwd
from torch.cuda.amp import custom_bwd
from torch.cuda.amp import GradScaler
from torch import nn, optim
from torch.nn import functional as F
from torchvision import transforms
from torchvision.transforms import functional as TF
from torch.cuda import get_device_properties

import torch_optimizer

import imageio

from PIL import ImageFile, Image, PngImagePlugin, ImageChops
ImageFile.LOAD_TRUNCATED_IMAGES = True

from subprocess import Popen, PIPE
import re

from torchvision.datasets import CIFAR100



####################################################
# main class used to start up the vqgan clip stuff, and allow for interactable generation
# - basic usage can be seen from generate.py
####################################################

class Hallucinator:

    def __init__(self, clipModel:str = 'ViT-B/32', vqgan_config_path:str = 'checkpoints/vqgan_imagenet_f16_16384.yaml', vqgan_checkpoint_path:str = 'checkpoints/vqgan_imagenet_f16_16384.ckpt', 
                 use_mixed_precision:bool = False, clip_cpu:bool = False, randomSeed:int = None, cuda_device:str = "cuda:0", anomaly_checker:bool = False, deterministic:int = 0, 
                 log_clip:bool = False, log_clip_oneshot:bool = False, log_mem:bool = False, display_freq:int = 50 ):

        ## passed in settings
        self.clip_model = clipModel
        self.vqgan_config_path = vqgan_config_path
        self.vqgan_checkpoint_path = vqgan_checkpoint_path
        self.use_mixed_precision = use_mixed_precision
        self.clip_cpu = clip_cpu
        self.seed = randomSeed
        self.cuda_device = cuda_device
        self.anomaly_checker = anomaly_checker
        self.deterministic = deterministic
        self.log_clip = log_clip
        self.log_clip_oneshot = log_clip_oneshot
        self.log_mem = log_mem
        self.display_freq = display_freq

        #### class wide variables set with default values
        self.clipPerceptorInputResolution = None # set after loading clip
        self.clipPerceptor = None # clip model
        self.clipDevice = None # torch device clip model is loaded onto
        self.clipCifar100 = None #one shot clip model classes, used when logging clip info

        self.vqganDevice = None #torch device vqgan model is loaded onto
        self.vqganModel = None #vqgan model
        self.vqganGumbelEnabled = False #vqgan gumbel model in use
        
        # From imagenet - Which is better?
        #normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
        #                                  std=[0.229, 0.224, 0.225])
        self.normalize = transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073],
                                        std=[0.26862954, 0.26130258, 0.27577711])


        # terrible hack
        model.do_nan_check = self.use_mixed_precision

    #############
    ## Life cycle
    #############

    # does the minimal initialization that we shouldnt need to reset, unless we
    # force a change in clip/torch/vqgan models
    def Initialize(self):
        self.InitTorch()        
        self.InitVQGAN()
        self.InitClip()
        print('Using vqgandevice:', self.vqganDevice)
        print('Using clipdevice:', self.clipDevice)

    ##################
    ### Logging and other internal helper methods...
    ##################
    def seed_torch(self, seed:int=42):
        random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        np.random.seed(seed % (2**32 - 1))
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed) # if you are using multi-GPU.

    def log_torch_mem(self, title:str = ''):
        t = torch.cuda.get_device_properties(0).total_memory
        r = torch.cuda.memory_reserved(0)
        a = torch.cuda.memory_allocated(0)
        f = r-a  # free inside reserved

        if title != '':
            print('>>>>  ' + title)

        print("total     VRAM:  " + str(t))
        print("reserved  VRAM:  " + str(r))
        print("allocated VRAM:  " + str(a))
        print("free      VRAM:  " + str(f))

        if title != '':
            print('>>>>  /' + title)


    ###################
    # Vector quantize
    ###################
    def vector_quantize(self, x, codebook) -> torch.Tensor:
        d = x.pow(2).sum(dim=-1, keepdim=True) + codebook.pow(2).sum(dim=1) - 2 * x @ codebook.T
        indices = d.argmin(-1)
        x_q = F.one_hot(indices, codebook.shape[0]).to(d.dtype) @ codebook
        return GenerateJob.replace_grad(x_q, x)

    def synth(self, z, gumbelMode) -> torch.Tensor:
        if gumbelMode:
            z_q = self.vector_quantize(z.movedim(1, 3), self.vqganModel.quantize.embed.weight).movedim(3, 1)
        else:
            z_q = self.vector_quantize(z.movedim(1, 3), self.vqganModel.quantize.embedding.weight).movedim(3, 1)
        return makeCutouts.clamp_with_grad(self.vqganModel.decode(z_q).add(1).div(2), 0, 1)



    ########################
    # get the optimiser ###
    ########################
    def get_optimiser(self, quantizedImg:torch.Tensor, opt_name:str, opt_lr:float):

        # from nerdy project, potential learning rate tweaks?
        # Messing with learning rate / optimisers
        #variable_lr = args.step_size
        #optimiser_list = [['Adam',0.075],['AdamW',0.125],['Adagrad',0.2],['Adamax',0.125],['DiffGrad',0.075],['RAdam',0.125],['RMSprop',0.02]]


        if opt_name == "Adam":
            opt = optim.Adam([quantizedImg], lr=opt_lr)	# LR=0.1 (Default)
        elif opt_name == "AdamW":
            opt = optim.AdamW([quantizedImg], lr=opt_lr)	
        elif opt_name == "Adagrad":
            opt = optim.Adagrad([quantizedImg], lr=opt_lr)	
        elif opt_name == "Adamax":
            opt = optim.Adamax([quantizedImg], lr=opt_lr)	
        elif opt_name == "DiffGrad":
            opt = torch_optimizer.DiffGrad([quantizedImg], lr=opt_lr, eps=1e-9, weight_decay=1e-9) # NR: Playing for reasons
        elif opt_name == "AdamP":
            opt = torch_optimizer.AdamP([quantizedImg], lr=opt_lr)		    	    
        elif opt_name == "RMSprop":
            opt = optim.RMSprop([quantizedImg], lr=opt_lr)
        elif opt_name == "MADGRAD":
            opt = torch_optimizer.MADGRAD([quantizedImg], lr=opt_lr)      
        else:
            print("Unknown optimiser. Are choices broken?")
            opt = optim.Adam([quantizedImg], lr=opt_lr)
        return opt


    ##########################
    ### One time init things... parsed from passed in args
    ##########################

    def InitTorch(self):
        print("Using pyTorch: " + str( torch.__version__) )
        print("Using mixed precision: " + str(self.use_mixed_precision) )  

        #TODO hacky as fuck
        makeCutouts.use_mixed_precision = self.use_mixed_precision

        if self.seed is None:
            self.seed = torch.seed()

        print('Using seed:', self.seed)
        self.seed_torch(self.seed)

        if self.deterministic >= 2:
            print("Determinism at max: forcing a lot of things so this will work, no augs, non-pooling cut method, bad resampling")

            # need to make cutouts use deterministic stuff... probably not a good way
            makeCutouts.deterministic = True

            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False # NR: True is a bit faster, but can lead to OOM. False is more deterministic.

            torch.use_deterministic_algorithms(True)	   # NR: grid_sampler_2d_backward_cuda does not have a deterministic implementation   

            # CUBLAS determinism:
            # Deterministic behavior was enabled with either `torch.use_deterministic_algorithms(True)` or `at::Context::setDeterministicAlgorithms(true)`, 
            # but this operation is not deterministic because it uses CuBLAS and you have CUDA >= 10.2. To enable deterministic behavior in this case, 
            # you must set an environment variable before running your PyTorch application: CUBLAS_WORKSPACE_CONFIG=:4096:8 or CUBLAS_WORKSPACE_CONFIG=:16:8. 
            # For more information, go to https://docs.nvidia.com/cuda/cublas/index.html#cublasApi_reproducibility  
            #   
            # set a debug environment variable CUBLAS_WORKSPACE_CONFIG to ":16:8" (may limit overall performance) or ":4096:8" (will increase library footprint in GPU memory by approximately 24MiB).
            os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'
            #EXPORT CUBLAS_WORKSPACE_CONFIG=:4096:8

            # from nightly build for 1.11 -> 0 no warn, 1 warn, 2 error
            # torch.set_deterministic_debug_mode(2)
        elif self.deterministic == 1:
            print("Determinism at medium: cudnn determinism and benchmark disabled")
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False # NR: True is a bit faster, but can lead to OOM. False is more deterministic.
        else:
            print("Determinism at minimum: cudnn benchmark on")
            torch.backends.cudnn.benchmark = True #apparently slightly faster, but less deterministic  

        if self.use_mixed_precision==True:
            print("Hallucinator mxed precision mode enabled: cant use augments in mixed precision mode yet")

        # Fallback to CPU if CUDA is not found and make sure GPU video rendering is also disabled
        # NB. May not work for AMD cards?
        if not self.cuda_device == 'cpu' and not torch.cuda.is_available():
            self.cuda_device = 'cpu'
            print("Warning: No GPU found! Using the CPU instead. The iterations will be slow.")
            print("Perhaps CUDA/ROCm or the right pytorch version is not properly installed?")     

        if self.anomaly_checker:
            torch.autograd.set_detect_anomaly(True)


    def InitClip(self):
        if self.log_clip:
            print("logging clip probabilities at end, loading vocab stuff")
            cifar100 = CIFAR100(root=".", download=True, train=False)

        jit = False
        try:
            # try here, since using nightly build of pytorch has a version scheme like dev23723h
            if [int(n) for n in torch.__version__.split(".")] < [1, 8, 1]:
                jit = True
        except:
            jit = False

        print( "available clip models: " + str(clip.available_models() ))
        print("CLIP jit: " + str(jit))
        print("using clip model: " + self.clip_model)

        if self.clip_cpu == False:
            self.clipDevice = self.vqganDevice
            if jit == False:
                self.clipPerceptor = clip.load(self.clip_model, jit=jit, download_root="./clipModels/")[0].eval().requires_grad_(False).to(self.clipDevice)
            else:
                self.clipPerceptor = clip.load(self.clip_model, jit=jit, download_root="./clipModels/")[0].eval().to(self.clipDevice)    
        else:
            self.clipDevice = torch.device("cpu")
            self.clipPerceptor = clip.load(self.clip_model, "cpu", jit=jit)[0].eval().requires_grad_(False).to(self.clipDevice) 



        print("---  CLIP model loaded to " + str(self.clipDevice) +" ---")
        self.log_torch_mem()
        print("--- / CLIP model loaded ---")

        self.clipPerceptorInputResolution = self.clipPerceptor.visual.input_resolution



    def InitVQGAN(self):
        self.vqganDevice = torch.device(self.cuda_device)

        self.vqganGumbelEnabled = False
        config = OmegaConf.load(self.vqgan_config_path)

        print("---  VQGAN config " + str(self.vqgan_config_path))    
        print(yaml.dump(OmegaConf.to_container(config)))
        print("---  / VQGAN config " + str(self.vqgan_config_path))

        if config.model.target == 'taming.models.vqgan.VQModel':
            self.vqganModel = vqgan.VQModel(**config.model.params)
            self.vqganModel.eval().requires_grad_(False)
            self.vqganModel.init_from_ckpt(self.vqgan_checkpoint_path)
        elif config.model.target == 'taming.models.vqgan.GumbelVQ':
            self.vqganModel = vqgan.GumbelVQ(**config.model.params)
            self.vqganModel.eval().requires_grad_(False)
            self.vqganModel.init_from_ckpt(self.vqgan_checkpoint_path)
            self.vqganGumbelEnabled = True
        elif config.model.target == 'taming.models.cond_transformer.Net2NetTransformer':
            parent_model = cond_transformer.Net2NetTransformer(**config.model.params)
            parent_model.eval().requires_grad_(False)
            parent_model.init_from_ckpt(self.vqgan_checkpoint_path)
            self.vqganModel = parent_model.first_stage_model
        else:
            raise ValueError(f'unknown model type: {config.model.target}')
        del self.vqganModel.loss   

        self.vqganModel.to(self.vqganDevice)

        print("---  VQGAN model loaded ---")
        self.log_torch_mem()
        print("--- / VQGAN model loaded ---")


    ################################
    ## clip one shot analysis, just for fun, probably done wrong
    ###############################
    @torch.inference_mode()
    def WriteLogClipResults(self, genJob:GenerateJob.GenerationJob, imgout:torch.Tensor):
        #TODO properly manage initing the cifar100 stuff here if its not already

        img = self.normalize(self.CurrentCutoutMethod(imgout))

        if self.log_clip_oneshot:
            #one shot identification
            image_features = self.clipPerceptor.encode_image(img).float()

            text_inputs = torch.cat([clip.tokenize(f"a photo of a {c}") for c in self.clipCifar100.classes]).to(self.clipDevice)
            
            text_features = self.clipPerceptor.encode_text(text_inputs).float()
            text_features /= text_features.norm(dim=-1, keepdim=True)

            # Pick the top 5 most similar labels for the image
            image_features /= image_features.norm(dim=-1, keepdim=True)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = (100.0 * image_features @ text_features.T).softmax(dim=-1)
            values, indices = similarity[0].topk(5)

            # Print the result
            print("\nOne-shot predictions:\n")
            for value, index in zip(values, indices):
                print(f"{self.clipCifar100.classes[index]:>16s}: {100 * value.item():.2f}%")

        if self.log_clip:
            # prompt matching percentages
            textins = []
            promptPartStrs = []
            if genJob.config.prompts:
                for prompt in genJob.config.prompts:
                    txt, weight, stop = genJob.split_prompt(prompt)  
                    splitTxt = txt.split()
                    for stxt in splitTxt:   
                        promptPartStrs.append(stxt)       
                        textins.append(clip.tokenize(stxt))
                    for i in range(len(splitTxt) - 1):
                        promptPartStrs.append(splitTxt[i] + " " + splitTxt[i + 1])       
                        textins.append(clip.tokenize(splitTxt[i] + " " + splitTxt[i + 1]))
                    for i in range(len(splitTxt) - 2):
                        promptPartStrs.append(splitTxt[i] + " " + splitTxt[i + 1] + " " + splitTxt[i + 2])       
                        textins.append(clip.tokenize(splitTxt[i] + " " + splitTxt[i + 1] + " " + splitTxt[i + 2]))                    

            text_inputs = torch.cat(textins).to(self.clipDevice)
            
            image_features = self.clipPerceptor.encode_image(img).float()
            text_features = self.clipPerceptor.encode_text(text_inputs).float()
            text_features /= text_features.norm(dim=-1, keepdim=True)

            # Pick the top 5 most similar labels for the image
            image_features /= image_features.norm(dim=-1, keepdim=True)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = (100.0 * image_features @ text_features.T).softmax(dim=-1)
            
            top = 5
            if top > similarity[0].size()[0]:
                top = similarity[0].size()[0]

            values, indices = similarity[0].topk(top)

            # Print the result
            print("\nPrompt matching predictions:\n")
            for value, index in zip(values, indices):        
                print(f"{promptPartStrs[index]:>16s}: {100 * value.item():.2f}%")   



    ######################
    ### interactive generation steps and training
    ######################

    def ProcessJobFullProfile(self, genJob:GenerateJob.GenerationJob, trainCallback = None):
        with torch.autograd.profiler.profile(use_cuda=True, with_stack=True) as prof:
            self.ProcessJobFull(genJob, trainCallback)

        #group_by_stack_n=5
        print("=========== CPU SELF =================")
        print( prof.key_averages().table(sort_by="self_cpu_time_total"))
        print("=========== CUDA SELF =================")
        print( prof.key_averages().table(sort_by="self_cuda_time_total"))
        print("=========== STACKs    =================")
        print( prof.key_averages(group_by_stack_n=60).table(sort_by="self_cuda_time_total"))

    def ProcessJobFull(self, genJob:GenerateJob.GenerationJob, trainCallback = None):        
            moreWork = True
            with tqdm() as pbar:
                while moreWork:   
                    # Training time         
                    moreWork = self.ProcessJobStep(genJob, trainCallback )
                    pbar.update()


    # step a job, returns true if theres more processing left for it
    def ProcessJobStep(self, genJob:GenerateJob.GenerationJob, trainCallbackFunc = None) -> bool:
        # Change text prompt
        if genJob.prompt_frequency > 0:
            if genJob.currentIteration % genJob.prompt_frequency == 0 and genJob.currentIteration > 0:
                # In case there aren't enough phrases, just loop
                if genJob.phraseCounter >= len(genJob.all_phrases):
                    genJob.phraseCounter = 0
                
                pMs = []
                genJob.prompts = genJob.all_phrases[genJob.phraseCounter]

                # Show user we're changing prompt                                
                print(genJob.prompts)
                
                for prompt in genJob.prompts:
                    genJob.EmbedTextPrompt(prompt)

                genJob.phraseCounter += 1
        
        #image manipulations before training is called, such as the zoom effect
        self.OnPreTrain(genJob, genJob.currentIteration)

        # Training time
        img, lossAll, lossSum = self.train(genJob, genJob.currentIteration)

        if trainCallbackFunc != None:
            trainCallbackFunc(genJob, genJob.currentIteration, img, lossAll, lossSum)
        
        self.DefaultTrainCallback(genJob, genJob.currentIteration, img, lossAll, lossSum)
   
        # Ready to stop yet?
        if genJob.currentIteration == genJob.totalIterations:
            self.OnFinishGeneration(genJob, genJob.currentIteration)    
            return False           

        genJob.currentIteration += 1
        return True

    
    @torch.inference_mode()
    def DefaultTrainCallback(self, genJob:GenerateJob.GenerationJob, iteration:int, curImg, lossAll, lossSum):
        # stat updates and progress images
        if iteration % self.display_freq == 0 and iteration != 0:
            self.DefaultCheckinLogging(genJob, iteration, lossAll, curImg)  

        if iteration % genJob.save_freq == 0 and iteration != 0:     
            if genJob.save_seq == True:
                genJob.savedImageCount = genJob.savedImageCount + 1                
            else:
                genJob.savedImageCount = iteration
                
            genJob.SaveImageTensor( curImg, str(genJob.savedImageCount).zfill(5))
                            
        if genJob.save_best == True:

            lossAvg = lossSum / len(lossAll)

            if genJob.bestErrorScore > lossAvg.item():
                print("saving image for best error: " + str(lossAvg.item()))
                genJob.bestErrorScore = lossAvg
                genJob.SaveImageTensor( curImg, "lowest_error_")


    @torch.inference_mode()
    def DefaultCheckinLogging(self, genJob:GenerateJob.GenerationJob, i:int, losses, out):
        print("\n*************************************************")
        print(f'i: {i}, loss sum: {sum(losses).item():g}')
        print("*************************************************")

        promptNum = 0
        lossLen = len(losses)
        if genJob.embededPrompts and lossLen <= len(genJob.embededPrompts):
            for loss in losses:            
                print( "----> " + genJob.embededPrompts[promptNum].TextPrompt + " - loss: " + str( loss.item() ) )
                promptNum += 1
        else:
            print("mismatch in prompt numbers and losses!")

        print(" ")

        if self.log_clip:
            self.WriteLogClipResults(out)
            print(" ")

        if self.log_mem:
            self.log_torch_mem()
            print(" ")

        print(" ")
        sys.stdout.flush()       



    def ascend_txt(self, genJob:GenerateJob.GenerationJob, iteration:int, synthedImage:torch.Tensor):
        with torch.cuda.amp.autocast(self.use_mixed_precision):

            cutouts = genJob.GetCutouts(synthedImage)

            if self.clipDevice != self.vqganDevice:
                clipEncodedImage = self.clipPerceptor.encode_image(self.normalize(cutouts.to(self.clipDevice))).float()
            else:
                clipEncodedImage = self.clipPerceptor.encode_image(self.normalize(cutouts)).float()

            
            result = genJob.GetCutoutResults(clipEncodedImage, iteration)
            
            return result # return loss


    def train(self, genJob:GenerateJob.GenerationJob, iteration:int):
        with torch.cuda.amp.autocast(self.use_mixed_precision):
            genJob.optimiser.zero_grad(set_to_none=True)
            
            synthedImage = self.synth(genJob.quantizedImage, genJob.vqganGumbelEnabled) 
            
            lossAll = self.ascend_txt(genJob, iteration, synthedImage)
            lossSum = sum(lossAll)

            if genJob.optimiser == "MADGRAD":
                genJob.loss_idx.append(lossSum.item())
                if iteration > 100: #use only 100 last looses to avg
                    avg_loss = sum(self.loss_idx[iteration-100:])/len(self.loss_idx[iteration-100:]) 
                else:
                    avg_loss = sum(self.loss_idx)/len(self.loss_idx)

                genJob.scheduler.step(avg_loss)
            
            if self.use_mixed_precision == False:
                lossSum.backward()
                genJob.optimiser.step()
            else:
                genJob.gradScaler.scale(lossSum).backward()
                genJob.gradScaler.step(genJob.optimiser)
                genJob.gradScaler.update()
            
            with torch.inference_mode():
                genJob.quantizedImage.copy_(genJob.quantizedImage.maximum(genJob.z_min).minimum(genJob.z_max))

            return synthedImage, lossAll, lossSum



    #########################
    ### do manipulations to the image sent to vqgan prior to training steps
    ### for example, image mask lock, or the image zooming effect
    #########################
    def OnPreTrain(self, genJob:GenerateJob.GenerationJob, iteration:int):
        for modContainer in genJob.ImageModifiers:
            if modContainer.ShouldApply( GenerationMods.GenerationModStage.PreTrain, iteration ):
                modContainer.OnPreTrain( iteration )

    def OnFinishGeneration(self, genJob:GenerateJob.GenerationJob, iteration:int):
        for modContainer in genJob.ImageModifiers:
            if modContainer.ShouldApply( GenerationMods.GenerationModStage.FinishedGeneration, iteration ):
                modContainer.OnPreTrain( iteration )

    